# Browser end-to-end tests

What a person actually does, done by a browser: open the interface, send a
message, watch a turn arrive, answer what it stops to ask, and check that what
was allowed actually happened.

```
npm --prefix apps/web ci
npx playwright install chromium        # once per machine
npm --prefix apps/web run test:e2e
```

The client is built for you before the run (`e2e/build-client.ts`), because the
Python process serves Vite's output and a stale `dist/` would mean testing the
previous commit's interface.

## How it is put together

Each test gets its own application, composed by `harness/server.py` exactly the
way `engine.apps.web.__main__` composes the real one -- same capabilities, same
runner mapping, same approval policy plumbing. Only four things are the test's,
and each is something a test run must not share or send anywhere:

| what | why |
| --- | --- |
| a fixture git repository, and a bare `origin` beside it | conversations and runs make worktrees of it, and a run bases its worktree on `origin/main` |
| a SQLite file under the test's own directory | one test's chats must not be another's |
| scripted `codex` and `claude` executables | a model is the one part of this that cannot be asserted on |
| a `gh` that records instead of commenting | the reviewer leaves its findings on a pull request, and that is somebody's repository |

The fake CLIs are `tests/provider_fakes.py`, shared with the pytest tier that
runs the approval contract against them. They are not mocks of our adapters:
they are real subprocesses speaking Codex's app-server JSON-RPC and Claude
Code's stream-JSON control protocol, and they really run the commands they are
allowed to run. What a turn says and does comes from a JSON script the test
writes:

```ts
engine.script({
  title: "Recording an approval",
  scenarios: [
    {
      when: "greeting",                       // matched against the prompt
      steps: [
        { type: "say", text: "Reading the repository first." },
        { type: "run", command: "echo approved > allowed.txt" },
        { type: "say", text: "Wrote the file." },
      ],
    },
  ],
});
```

Scenarios are selected by what the turn was asked rather than by a counter, so
a title turn, a retry, or a second conversation cannot knock a script out of
step. The first matching scenario wins, which matters for a workflow: the
reviewer is quoted the original task, so its prompt contains the implementation
scenario's word too, and the one only a reviewer can match has to be listed
first. A turn run without the approval transport -- the runtime naming a chat or
a workflow -- is answered with `title` instead of a scenario.

A workflow step ends only when the agent calls `complete_step` or `fail_step` on
the run-bound MCP server the runtime attached to that turn, so the fakes are MCP
*clients* too:

```ts
{ type: "tool", name: "complete_step",
  arguments: { outcome: "success", summary: "Added the greeting.",
               outputs: { pr_url: "https://github.com/acme/api/pull/7" } } }
```

They read the server off argv the way each provider encodes it -- `--mcp-config`
for Claude, `-c mcp_servers.workflow.*` or the app-server thread config for
Codex -- spawn it as given, and make a real JSON-RPC `tools/call`. A completion
missing a declared output is refused by the runtime and the turn is corrected,
which `tests/test_workflow_integration.py` covers at the faster tier.

**End a workflow scenario with a `say`.** The runtime cancels the CLI as soon as
it accepts a terminal result, so the step usually ends mid-turn -- but when the
CLI finishes first, both adapters assemble the turn with its *last spoken text*
as the answer, which moves narration to the end. A turn whose last item is a
tool call therefore no longer matches what was streamed, and the runtime refuses
it (`streamed workflow transcript does not match completed turn`) even though
the result was accepted. A closing message is what a real CLI sends anyway, and
it keeps that race off these tests.

That is a workaround, and it is only here until #105 is fixed -- which is also
where the deterministic reproduction lives, since the race itself has never been
observed end to end. Take the closing `say` out with that ticket, not before.

A failing test keeps its directory and prints the path, and attaches whatever
the server said to the report. `npx playwright show-trace test-results/…` opens
the trace.

## Reading a run afterwards

Every run writes `playwright-report/`, pass or fail, and each spec attaches a
full-page still at every state it asserts on:

```ts
await shot(page, testInfo, "2 the approval, pending");
```

Numbered so the report reads as a sequence. They are documentation as much as
diagnostics -- what the approval card actually looked like on that commit, for
someone who is not going to run the tier -- so every spec added here should
attach the same kind of still at its own decisive moment.

```
npx playwright show-report apps/web/playwright-report
```

In CI the report is uploaded from the `browser` job whether or not the run went
green. It is a static site and GitHub will not serve it: download the artifact,
unzip it, and point `show-report` at the directory.

## What is covered

* `chat-approvals.spec.ts` -- a new chat on each runner: the turn streams while
  it is still running, the approval it pauses on reaches the browser, approving
  it is recorded as an approval, the turn carries on, and the file the command
  was allowed to write exists in that chat's worktree.
* `workflow-run.spec.ts` -- a workflow run on each runner, end to end: the run
  is created from the form, provisions a checkout that exists on disk, streams
  the implementation's first message and its command into the step's
  conversation *while the step is still running*, and -- once `complete_step`
  carries the declared `pr_url` -- advances through a review that leaves its
  finding on `gh` to "Action required". Approving there ends it, and a reload
  shows the same finished run: `succeeded`, `approved`, every stage behind it.
* `persisted-navigation.spec.ts` -- a cold start over a SQLite file populated
  through the production state-store adapter: the run list, run detail,
  implementation and review transcripts, and a multi-turn standalone chat are
  followed through their browser links. It then starts another chat and another
  workflow in the same database and confirms the older history remains listed.

## What the rest needs

The behaviours below are the ones we want next. Each names what has to exist
before it can be written; nothing here is a change to the product, except where
it says so.

### Workflow runs

1. **Questions.** Both providers can ask: Claude through `AskUserQuestion`,
   Codex through `item/tool/requestUserInput`. Both already normalize to
   `user_input` approvals with a modal in the client.
   *New script step: `{"type": "ask", "questions": [...]}`.*
2. **Plans.** Only Claude produces `plan_approval` today (`ExitPlanMode`); Codex
   has no app-server equivalent, so that test is Claude-only until it does.
   *New script step: `{"type": "plan", "plan": "…"}`.*

### The behaviours, once those exist

| behaviour | needs | notes |
| --- | --- | --- |
| approval propagates and approving executes | — | same card as the chat test, reached from the run page |
| agent asks for clarification | 1 | answering resumes the same agent run |
| reviewer adds review comments | — | assert against `engine.ghLog`, not GitHub; the reviewer is refused `complete_step` until it has left at least one comment |
| talking after review reopens implementation | — | `StepReactivated`; the composer is only offered on editable steps |
| auto-approve runs several requests unattended | — | toggle in the conversation header; script several `run` steps and assert `decisionSource` is not `user` |
| a failed workflow reads as failed | — | `fail_step`, and a CLI that exits nonzero -- they surface differently |
| a plan reaches the operator | 2 | Claude only |
| rejecting reopens the implementation | — | the correction loop: `Reject`, then `StepReactivated` and a second implementation turn |

## Live provider CLIs

This tier is deliberately deterministic: a scripted CLI is what makes "the
agent asked, the user approved, the file exists" a fact about our code rather
than about a model's mood. The live half already exists and belongs where it
is: `.github/workflows/cli-compatibility.yml` runs the same approval contract
against the pinned real `codex` and `claude` releases on a schedule.

If you want the browser tier pointed at a real CLI as well, the credentials go
in **repository → Settings → Secrets and variables → Actions**, under the names
that workflow already reads:

* `OPENAI_API_KEY` -- Codex CLI.
* `ANTHROPIC_API_KEY` -- Claude Code. A subscription token from
  `claude setup-token` works too, as `CLAUDE_CODE_OAUTH_TOKEN`; whichever you
  add, the job must export it into the server process's environment, because
  that is what spawns the CLI.

Absent, live scenarios skip rather than fail: an unauthenticated runner is a
configuration fact, not a test result. Nothing in this directory reads a
credential today.

## Tooling worth knowing

* `npx playwright test --ui` -- the run, the DOM, and the network at each step.
* `npx playwright show-trace test-results/…/trace.zip` -- the same, after the
  run. Locally every test keeps a trace, passing ones included, so the click
  that approved something can be replayed later; CI keeps failures only.
* `npx playwright codegen <url>` -- point it at a harness server you started by
  hand (`uv run python apps/web/e2e/harness/server.py --port 8123 --repository
  … --state …`) and click through it to author selectors.
* `ENGINE_E2E_PYTHON=/path/to/python npm run test:e2e` -- skip `uv run` per
  test when you already have a prepared interpreter.
* Specs currently reach for class names (`.approval-pending`, `.stat`). A small
  number of `data-testid` landmarks in the client would make them read better
  and break less; worth doing when the second or third spec wants the same
  element.
