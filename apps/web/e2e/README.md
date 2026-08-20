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
runner mapping, same approval policy plumbing. Only three things are the test's:

| what | why |
| --- | --- |
| a fixture git repository | conversations and runs make worktrees of it, and a temporary directory is a safe thing to leave worktrees in |
| a SQLite file under the test's own directory | one test's chats must not be another's |
| scripted `codex` and `claude` executables | a model is the one part of this that cannot be asserted on |

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
step. A turn run without the approval transport -- the runtime naming a chat or
a workflow -- is answered with `title` instead of a scenario.

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

## What the rest needs

Everything else -- workflow runs, review, reactivation, auto-approve, failure,
plans -- is written up as tickets in **`TICKETS.md`**, in dependency order, with
the files each one touches and what "done" means.

The short version: three prerequisites (an `origin` for provisioning to base
runs on, fake CLIs that can call `complete_step` over MCP, and a `gh` that is
not GitHub), then eight behaviour tickets that all depend on them. One of the
findings there is a product gap rather than a test one: nothing in the client
calls `POST /api/runs/{id}/human-review`, so a run cannot be finished through a
browser today.

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
