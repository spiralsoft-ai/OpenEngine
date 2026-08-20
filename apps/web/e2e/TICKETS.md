# Browser tier: the work after the first spec

`chat-approvals.spec.ts` covers one chat, one approval, two runners. What is
still uncovered is every workflow behaviour: provisioning, streaming into a run
conversation, `complete_step`, review, reactivation, auto-approve, failure, and
plans.

These are the tickets for that, in dependency order. **T1–T3 are prerequisites**
and buy nothing on their own; **T4–T11 are the behaviours**; **T12 is a product
gap** this work found. Each names the files it touches and what "done" means, so
one can be picked up without re-deriving how the system fits together.

Read `README.md` first for how the harness is put together. Facts referenced
below were checked against the tree at `f3caa83`; if a line number has moved,
the symbol name is the durable half.

| | ticket | depends on |
| --- | --- | --- |
| T1 | Give the harness an `origin` to base runs on | — |
| T2 | Teach the fake CLIs to call MCP tools | — |
| T3 | A `gh` that is not GitHub | — |
| T4 | Provision → implement → `complete_step` → review | T1, T2 |
| T5 | An approval inside a workflow conversation | T4 |
| T6 | A question reaches the operator and resumes the step | T4 |
| T7 | The reviewer adds review comments | T4, T3 |
| T8 | Talking after review reopens implementation | T4 |
| T9 | Auto-approve clears several requests unattended | T4 |
| T10 | A failed run reads as failed | T4 |
| T11 | A plan reaches the operator | T4 |
| T12 | Nothing in the client finishes a human review | — (product) |

One prerequisite is already done: **a run is readable without being re-run**
(#99). There is an HTML report every time, a trace of every local test, and
`shot(page, testInfo, "2 the approval, pending")` in `e2e/harness.ts` for
attaching a full-page still. Every ticket below says where to call it; the
number prefix is what makes the report read as a sequence.

---

## T1 — Give the harness an `origin` to base runs on

**Why.** `WORKFLOW_BASE_REF` is `"origin/main"`
(`packages/engine/src/engine/core/workflows/implementation_review.py:40`), and
the worktree provider treats that string specially: `_resolve_base` runs
`git fetch --no-tags origin +refs/heads/main:refs/engine/provisioning/<hex>`
against the repository root, resolves it, and deletes the temporary ref
(`.../workspace_provider/git_worktree/__init__.py:188`). The fixture repository
built by `fixtureRepository` in `e2e/harness.ts` has no remote, so provisioning
fails before a workflow run reaches implementation. Nothing downstream can be
tested until this is fixed.

**Work.** In `fixtureRepository`, after the first commit:

```
git init --bare <root>/origin.git
git -C <repository> remote add origin <root>/origin.git
git -C <repository> push origin main
```

Return both paths from the fixture (the test wants the repository; a later
ticket may want the bare one). Chat conversations do not go through this path
-- `ThreadService` provisions from the configured repository directly -- so
`chat-approvals.spec.ts` must keep passing unchanged.

**Done when** a test that POSTs `/api/runs` with
`{workflowId: "implementation-review-v1", prompt, repository}` reaches phase
`implementing` rather than `failed`, and the run's worktree exists on disk.

**Watch out for.** `_resolve_base` deletes its temporary ref in a `finally`, so
a failed fetch leaves no litter but also no diagnosis -- if this misbehaves,
read the server log the harness attaches, not the worktree.

---

## T2 — Teach the fake CLIs to call MCP tools

**Why.** This is the largest prerequisite and it unblocks T4 through T11.
Nothing ends a workflow step except a `complete_step` or `fail_step` call on the
run-bound MCP server the runtime attaches
(`packages/runtime/src/engine/runtime/terminal_mcp.py`). A step whose agent
merely stops gets corrected and re-prompted, and after two corrections the run
fails. So a fake that cannot make an MCP call cannot drive a workflow at all.

**How the server reaches the CLI.** The runtime builds an `McpServerConfig`
(`name`, `command`, `args`) and each adapter translates it:

* Claude -- `--mcp-config '{"mcpServers":{"<name>":{"command":…,"args":[…]}}}'`
  on argv, plus `--allowedTools mcp__<name>__complete_step` and siblings
  (`.../claude_code/__init__.py:522-548`).
* Codex -- `-c mcp_servers.<name>.command=<json>` and
  `-c mcp_servers.<name>.args=<json>` (`.../codex/__init__.py:716-723`).

The command is `engine.runtime.terminal_mcp_server` carrying an opaque token; it
speaks ordinary stdio MCP and forwards to the in-process broker that owns the
run.

**Work**, in `tests/provider_fakes.py`:

1. Parse the server out of argv -- one reader per provider, since the two encode
   the same three fields differently.
2. A minimal MCP client: spawn the command, send `initialize`, the
   `notifications/initialized` notification, then `tools/call` with the name and
   arguments, and read the result. Roughly thirty lines of JSON-RPC over pipes.
   No SDK: the point of these fakes is that they speak protocols we can read.
3. A new script step:

   ```json
   {"type": "tool", "name": "complete_step",
    "arguments": {"outcome": "success", "summary": "Added the greeting.",
                  "outputs": {"pr_url": "https://github.com/acme/api/pull/7"}}}
   ```

   `complete_step` requires `outcome`, `summary`, and `outputs`; `fail_step`
   requires `summary`; `add_comment` requires `pr_url` and `comment`, with
   `file` and `line` together or not at all (schemas at `terminal_mcp.py:209`).
   The implementation step's `required_outputs` is `("pr_url",)` and review's is
   `("findings",)`, so a script that omits them gets a correction rather than a
   completion -- which is itself worth one test.
4. Emit the call into the transcript the way a real CLI would (a `tool_use` and
   `tool_result` pair for Claude, a `commandExecution`-shaped item for Codex) so
   the browser can see that the agent called it.

**Done when** a *pytest* test -- not a browser one -- drives a full workflow run
against the fakes and reaches `awaiting_human_review`. Put it beside
`tests/test_workflow_integration.py`. This tier is much faster feedback than
Playwright and is where a protocol mistake should be caught; the browser tests
then only have to prove the interface.

**Watch out for.** The token is per-agent-run and the broker refuses an
unauthorized call (`terminal_mcp.py:151`), so the fake must pass through
whatever argv it was given rather than reconstructing it. And Claude will refuse
to *use* a tool it was not allowed -- if a call silently never happens, check
`--allowedTools` before suspecting the server.

---

## T3 — A `gh` that is not GitHub

**Why.** The reviewer's `add_comment` capability goes through
`GitHubSourceControl`, which shells out to `gh`
(`.../source_control/github/__init__.py:43-97`): `gh pr comment` for a general
comment, `gh pr view --json headRefOid` then `gh api …/pulls/<n>/comments` for
an inline one. T7 needs to assert on those calls without touching GitHub.

`binary_path` is a constructor argument, but the web composition passes only the
token (`apps/web/src/engine/apps/web/composition.py:110`), so today the only
lever is `PATH`.

**Work.** Add `github_binary: str = "gh"` to `Settings` and pass it through --
consistent with `codex_binary` and `claude_binary`, which exist for exactly this
reason. Then `harness/server.py` writes a fake `gh` next to the fake CLIs that
appends its argv and stdin to a JSONL the test reads, and exits 0 with plausible
output (`gh pr view --json headRefOid --jq .headRefOid` must print a SHA, or the
adapter raises).

Shimming `PATH` instead would work and touches no product code, but it is
invisible from the composition report and would break the moment something else
in the process wants the real `gh`. Prefer the field.

**Done when** an inline `add_comment` through the broker produces two recorded
`gh` invocations with the pull-request URL, body, path, line, and head SHA the
caller asked for.

---

## T4 — Provision → implement → `complete_step` → review

Covers **1a**, **1b**, and **1e** of the original list. Depends on T1, T2.

**The spec**, `e2e/workflow-run.spec.ts`, once per workflow runner:

1. Script two scenarios, selected by prompt substring -- the implementation
   prompt is the task text, the review prompt contains `"Review the completed
   implementation"`, so they can be told apart without a counter:

   ```ts
   engine.script({
     title: "Adding a greeting",
     scenarios: [
       { when: "greeting", steps: [
           { type: "say", text: "Reading the tree." },
           { type: "run", command: "echo hello > greeting.txt", approval: false },
           { type: "tool", name: "complete_step", arguments: { … } } ] },
       { when: "Review", steps: [
           { type: "say", text: "Looks right." },
           { type: "tool", name: "complete_step", arguments: { … } } ] },
     ],
   });
   ```

2. Drive `/runs/new` in the browser -- fields are labelled **Repository**,
   **Implementation runner**, **Task prompt**, button **Create workflow run**
   (`apps/web/src/runs.tsx:225-275`) -- and land on `/runs/{runId}`.

**Assert.**

* **Provisioning (1a).** The run page moves off `pending` through
  `preparing_workspace` to `implementing`; the checkout section
  (`.run-workspace`, `aria-label="Workflow checkout"`) names a path, and that
  path exists on disk. `shot(page, testInfo, "1 provisioned")`.
* **Streaming (1b).** Follow the implementation step's conversation link
  (`.link-flame`, `/runs/{run}/conversations/{instance}`) *while the step is
  still running*, and assert the agent's first message and the command it ran
  are on screen before the step completes. This is a genuinely different path
  from chat: a workflow conversation is served by
  `stream_workflow_conversation` (`api.py:939`), which **polls the durable
  transcript every 250 ms** and emits `content`, `approval`, and `done` frames.
  A test that only asserts the end state would pass with streaming broken.
  `shot(page, testInfo, "2 mid-step, streaming")`.
* **Advancing (1e).** After `complete_step`, the implementation step shows its
  declared output `pr_url` in `.step-outputs`, the review step starts, and the
  run reaches `awaiting_human_review` with the "Action required" callout.
  `shot(page, testInfo, "3 review reached")`.

**Done when** both runners pass and the assertions above hold on the *browser*,
not only through the API.

**Watch out for.** Naming runs on the non-interactive transport before
implementation starts, so the script's `title` must be set or the run is named
by the fallback. And review runs on the *review* runner for the same provider
(`build_review_runners`), which is read-only -- a review scenario that tries to
`run` a command is testing the wrong thing.

---

## T5 — An approval inside a workflow conversation

Covers **1c**. Depends on T4.

Same shape as the chat test, reached from the run page instead: script the
implementation step with `{ type: "run", command: "echo approved > allowed.txt" }`
(approval on, which is the default), open the step's conversation, and assert
the pending card (`.approval-pending`) carries the command, click **Approve**,
then assert `.approval-decided` reads `Approved · <command>`, the step goes on to
`complete_step`, and **the file exists in the run's worktree**. A still either
side of the click, as `chat-approvals.spec.ts` does.

Worth its own spec rather than folding into T4: the decision travels a different
route here (`POST /api/threads/{instance}/runs/current/approvals/{id}` resolves
against the *workflow* agent run -- `api.py:1293-1310`), and the pending card
arrives on the poll loop's `approval` frame rather than the chat run's stream.

**Done when** approving from the run conversation is recorded with
`decisionSource: "user"` and the approved command's effect is on disk.

---

## T6 — A question reaches the operator and resumes the step

Covers **1d**. Depends on T4.

Both providers can ask, and both normalize to a `user_input` approval:
Claude through the `AskUserQuestion` tool
(`.../claude_code/__init__.py:146-150`), Codex through
`item/tool/requestUserInput` (`.../codex/__init__.py:327`). The client renders
them as a modal, not a card -- `.question-modal`, `aria-label="Agent question"`,
with a fieldset per question, radio or checkbox options, and an optional "other"
text field (`apps/web/src/chat.tsx:554-630`).

**Work.** A new script step, `{"type": "ask", "questions": [...]}`, emitting each
provider's real request shape. Mirror the `UserInputQuestion` fields the
adapters parse: `id`, `header`, `question`, `options` (label + description),
`multiSelect`, `allowsOther`.

**Assert.** The modal appears with the question text; answering it submits; the
same agent run continues rather than a new one starting (compare the
conversation's agent-run id either side); and the answer reaches the agent --
easiest proved by scripting the next step to write the answer into a file, so
the assertion is on disk rather than on a protocol frame. A still of the modal:
it is a screen most people will not have seen.

**Also worth one case:** a single-select question with `allowsOther`, answered
through the "other" field, since that path builds the answer differently.

---

## T7 — The reviewer adds review comments

Covers **1f**. Depends on T4 and T3.

The review profile grants `add_comment` (`implementation_review.py:89`) and the
dispatcher enables the tool on the broker only when the profile asks for it
*and* the wired source control has the method (`dispatcher.py:278-290`). Two
behaviours follow, and both should be asserted:

1. A reviewer that calls `add_comment` with the `pr_url` the implementation step
   declared reaches the fake `gh` with that URL and body.
2. **A reviewer that tries to `complete_step` without commenting is refused** --
   `"add at least one pull-request comment before completing review"`
   (`terminal_mcp.py:172-177`) -- and the correction is what makes it comment.
   This is the more interesting half: it is a rule about review quality that
   nothing else tests end to end.

**Assert** against the fake `gh`'s recording, and on the run reaching
`awaiting_human_review` with the review step's `findings` output shown.

**Watch out for.** `pr_url` must be a real-looking GitHub pull-request URL:
`_pull_request_parts` rejects anything that is not
`https://github.com/<owner>/<repo>/pull/<number>` (`github/__init__.py:104`).

---

## T8 — Talking after review reopens implementation

Covers extra behaviour **1**. Depends on T4.

Once a run is `awaiting_human_review`, sending a message to the *implementation*
conversation calls `resume_implementation`, which transitions
`StepReactivated(step_id=IMPLEMENTATION_STEP)` and puts the run back into
`implementing` (`workflow_execution.py:146-168`). The review conversation is
durable and gets the new result appended as fresh context rather than a second
conversation being made.

**Assert.** Drive a run to `awaiting_human_review` (T4), open the implementation
conversation, and check the composer is *offered* -- the client hides it unless
the step is editable (`chat.tsx:966-972`) -- then send a message and assert: the
run phase returns to `implementing`, the step's chip follows, the same agent
instance is reused, and after a second `complete_step` the run reaches review
again with the *same* review conversation carrying both rounds. A still of the
reopened run.

**Also assert the negative:** the review conversation offers no composer, since
that is the rule this feature is a carve-out from.

---

## T9 — Auto-approve clears several requests unattended

Covers extra behaviour **2**. Depends on T4.

The toggle is a per-thread checkbox labelled **Auto-approve** in the
conversation header (`apps/web/src/main.tsx:243-250`), persisted with
`PATCH /api/threads/{id} {"autoApprove": true}`. Turning it on also settles
anything already pending (`api.py:694`). A request it answers is recorded as
`decisionSource: "policy"` -- deliberately distinct from `user` and
`session_grant`, so an audit can say "nobody, the configuration did"
(`approvals.py:209-226`).

**Assert.** Script three or four `run` steps that would each ask, turn the toggle
on, and check: every request is recorded decided, none of them with
`decisionSource: "user"`, every command's effect is on disk, and the turn never
parks. A still of the settled list -- several `.approval-decided` cards in a row
is exactly the screenshot somebody wants of this feature.

**And assert what auto-approve does *not* cover:** `_policy_decision` returns
`None` for any request marked `requires_human` (`approvals.py:564`), so a
question or a plan still stops the run with the toggle on. Script one after the
commands and check the modal still appears. That is the property that makes the
feature safe, and the one a regression would quietly remove.

**Also worth covering:** turning the toggle on *while* a request is pending
settles it, which is a different code path from deciding one that arrives later.

---

## T10 — A failed run reads as failed

Covers extra behaviour **3**. Depends on T4.

Three failures surface differently, and the ticket is to tell them apart:

1. **`fail_step`** -- the agent's own report that it cannot continue. Run phase
   `failed`, and the summary it gave is what the run page shows.
2. **A CLI that exits nonzero** mid-turn -- the adapter raises, and
   `_fail` records it (`workflow_execution.py:143-145`). Script this with a step
   that makes the fake exit nonzero.
3. **A step that never calls a terminal tool** -- corrected twice, then failed.
   Slowest of the three; assert the corrections happened rather than only the end
   state.

**Assert.** For each: the run page's phase chip reads `failed` with the accent
(`phaseAccent` returns `"flame"`), the runs list shows it, and the failure
summary is on the page rather than only in the log. A still of each -- three
failures that look the same on screen would itself be the finding.

**Watch out for.** Case 3 takes as long as three turns; give it room under the
90-second per-test timeout or script the corrections to be quick.

---

## T11 — A plan reaches the operator

Covers extra behaviour **4**. Depends on T4. **Claude only.**

`ExitPlanMode` normalizes to `ApprovalKind.PLAN_APPROVAL`
(`.../claude_code/__init__.py:146-151`), which the client labels "Wants approval
for a plan" (`chat.tsx:391`). Codex's app-server has no equivalent request, so
there is nothing to script for it -- write the test Claude-only and say so in a
comment, rather than leaving a reader to wonder whether the Codex case was
forgotten.

**Work.** A `{"type": "plan", "plan": "…"}` script step emitting the
`ExitPlanMode` tool use and its permission request.

**Assert.** The card renders with the plan text and the plan-approval label,
approving it lets the turn continue, and rejecting it does not. A still of the
card -- this is the other screen most people will never have seen.

---

## T12 — Nothing in the client finishes a human review

**A product gap, not a test.** `POST /api/runs/{run_id}/human-review` exists
(`api.py:1351`), and the run page renders the "Action required" callout with the
pending step's title and summary (`runs.tsx:436-446`) -- but no control in the
client calls that endpoint. A run therefore cannot be driven to completion
through a browser: it stops at `awaiting_human_review` forever.

Every ticket above stops at that phase for the same reason. Whether to build the
control now or accept the ceiling is a product decision; it should be made before
T4 is written, because the answer changes what "the end of a run" means for the
whole suite.

---

## Notes that apply to all of them

**Selectors.** The specs reach for class names (`.approval-pending`, `.stat`,
`.step-outputs`). That was fine for one spec; by the third it is worth adding a
few `data-testid` landmarks to the client so these read better and break less.
Do it when the second ticket wants the same element, not before.

**Where a behaviour belongs.** If a property can be proved in pytest, prove it
there -- it is seconds rather than a minute, and the failure points at the code
rather than at a page. The browser tier is for what only a browser can answer:
did this reach the screen, could a person act on it, and did acting work.

**Live CLIs.** This tier stays scripted on purpose. The live half exists in
`.github/workflows/cli-compatibility.yml`, on a schedule, with the credentials
described in `README.md`.
