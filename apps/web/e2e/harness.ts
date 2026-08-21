/** One server per test, wired to provider CLIs the test writes the script for.
 *
 *  Everything between the click and the subprocess is the real thing: the
 *  Python application, its SQLite store, its git worktrees, and both provider
 *  protocols. The model is the only part replaced, because a model is the one
 *  part of this that cannot be asserted on.
 *
 *  Per test rather than per run: a conversation, a worktree, and a database are
 *  all things one test leaves behind, and sharing them would make failures
 *  depend on what ran first. */

import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { test as base, type Page, type TestInfo } from "@playwright/test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");
const SERVER = path.join(HERE, "harness", "server.py");
const SEED = path.join(HERE, "harness", "seed.py");

/** How long a composed server may take to answer. Long enough for `uv run` to
 *  resolve the workspace on a cold machine, short enough to fail rather than
 *  hang out a whole CI job. */
const STARTUP_TIMEOUT_MS = 90_000;

export type ScriptStep =
  | { type: "say"; text: string }
  | { type: "run"; command: string; approval?: boolean }
  /** A call on the run-bound MCP server the runtime attached to this turn.
   *  `complete_step` and `fail_step` are what end a workflow step; a reviewer
   *  additionally has `add_comment`. */
  | { type: "tool"; name: string; arguments: Record<string, unknown> };

export type Scenario = {
  /** Matched as a substring of the prompt. Absent, it matches any turn. */
  when?: string;
  steps: ScriptStep[];
};

export type Script = {
  /** What a naming turn answers, and therefore the conversation's title. */
  title?: string;
  scenarios: Scenario[];
};

export class Engine {
  constructor(
    readonly url: string,
    /** The git repository conversations and workflow runs work in. */
    readonly repository: string,
    /** The bare repository it pushes to, which workflow runs base themselves on. */
    readonly origin: string,
    /** Every `gh` invocation the server made, one `{argv, stdin}` per line. */
    readonly ghLog: string,
    private readonly scriptPath: string,
  ) {}

  /** What the agent will say and do on its next turn.
   *
   *  Read by the CLI when it runs, so a test can rewrite it between turns
   *  without restarting anything. */
  script(script: Script): void {
    writeFileSync(this.scriptPath, JSON.stringify(script, null, 2), "utf-8");
  }
}

export const test = base.extend<{ engine: Engine; seededDatabase: boolean }>({
  seededDatabase: [false, { option: true }],
  engine: async ({ seededDatabase }, use, testInfo) => {
    const root = mkdtempSync(path.join(tmpdir(), "engine-e2e-"));
    const state = path.join(root, "state");
    mkdirSync(state);
    const { repository, origin } = fixtureRepository(root);
    if (seededDatabase) seedState(state, repository);
    const scriptPath = path.join(root, "script.json");
    const engine = new Engine(
      `http://127.0.0.1:${await freePort()}`,
      repository,
      origin,
      path.join(state, "gh.jsonl"),
      scriptPath,
    );
    engine.script({
      scenarios: [{ steps: [{ type: "say", text: "This turn was not scripted." }] }],
    });

    const server = startServer(engine.url, repository, state, scriptPath);
    try {
      await waitUntilServing(engine.url, server);
      await use(engine);
    } finally {
      await stop(server);
      if (testInfo.status !== testInfo.expectedStatus) {
        // The failure is usually explained by what the server said, and the
        // directory holds the transcript, the database, and the worktree.
        await testInfo.attach("server", {
          body: server.log.join(""),
          contentType: "text/plain",
        });
        console.log(`[engine] kept the failed run's directory: ${root}`);
      } else {
        rmSync(root, { recursive: true, force: true });
      }
    }
  },
  baseURL: async ({ engine }, use) => {
    await use(engine.url);
  },
});

export { expect } from "@playwright/test";

/** Attach a full-page still to the report, under `name`.
 *
 *  Documentation as much as diagnostics: a passing run's report is where
 *  somebody goes to see what the interface does at a moment they cannot
 *  reproduce on demand, and a screenshot is readable without replaying a
 *  trace. Every spec should attach one at each state it asserts on, named so
 *  the report reads as a sequence. */
export async function shot(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  await testInfo.attach(name, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
}

/** A repository with one commit and an origin, for chats and runs to work in.
 *
 *  The bare origin is what makes this repository something a *workflow* can run
 *  in: a run bases its worktree on `origin/main`, which the provider resolves by
 *  fetching from the remote of that name. A chat needs none of this -- it is
 *  provisioned from the configured repository directly -- so before there was an
 *  origin, provisioning failed before a run ever reached its first agent. */
function fixtureRepository(root: string): { repository: string; origin: string } {
  const repository = path.join(root, "repository");
  const origin = path.join(root, "origin.git");
  mkdirSync(repository);
  execFileSync("git", ["init", "--initial-branch=main", repository], { stdio: "pipe" });
  writeFileSync(path.join(repository, "README.md"), "# fixture\n", "utf-8");
  const git = (...args: string[]) =>
    execFileSync("git", ["-C", repository, ...args], { stdio: "pipe" });
  git("add", "--all");
  git(
    "-c",
    "user.email=engine@localhost",
    "-c",
    "user.name=engine",
    "commit",
    "--no-verify",
    "--message",
    "fixture repository",
  );
  execFileSync("git", ["init", "--bare", "--initial-branch=main", origin], {
    stdio: "pipe",
  });
  git("remote", "add", "origin", origin);
  git("push", "origin", "main");
  return { repository, origin };
}

/** A composed application, and everything it said while it was running. */
type Server = {
  process: ChildProcess;
  log: string[];
  /** Set when the process could not be started at all. */
  failure?: string;
};

function startServer(
  url: string,
  repository: string,
  state: string,
  scriptPath: string,
): Server {
  // `uv run` composes the workspace the way every other entry point does. A
  // prepared interpreter is offered as an override for anyone who would rather
  // not pay for that on every test.
  const python = process.env.ENGINE_E2E_PYTHON;
  const [command, ...arguments_] = python
    ? [python, SERVER]
    : ["uv", "run", "--frozen", "python", SERVER];
  const started = spawn(
    command,
    [
      ...arguments_,
      "--port",
      new URL(url).port,
      "--repository",
      repository,
      "--state",
      state,
    ],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, ENGINE_FAKE_SCRIPT: scriptPath },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const server: Server = { process: started, log: [] };
  started.stdout?.on("data", (chunk: Buffer) => server.log.push(chunk.toString()));
  started.stderr?.on("data", (chunk: Buffer) => server.log.push(chunk.toString()));
  // A missing `uv` never exits, because it never started. Waiting the whole
  // startup timeout to say so would report a mistyped command as a slow one.
  started.on("error", (error) => {
    server.failure = `${command} could not be started: ${error.message}`;
  });
  return server;
}

/** Populate the test's SQLite file through the production state-store adapter.
 *
 *  This runs before the web process opens the database, making the navigation
 *  spec a genuine cold start over existing state rather than data it created
 *  through the server it is about to inspect. */
function seedState(state: string, repository: string): void {
  const python = process.env.ENGINE_E2E_PYTHON;
  if (python) {
    execFileSync(python, [SEED, "--state", state, "--repository", repository], {
      cwd: REPO_ROOT,
      stdio: "pipe",
    });
    return;
  }
  execFileSync(
    "uv",
    ["run", "--frozen", "python", SEED, "--state", state, "--repository", repository],
    { cwd: REPO_ROOT, stdio: "pipe" },
  );
}

async function waitUntilServing(url: string, server: Server) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (server.failure) throw new Error(server.failure);
    if (server.process.exitCode !== null)
      throw new Error(
        `the server exited ${server.process.exitCode}:\n${server.log.join("")}`,
      );
    try {
      const response = await fetch(`${url}/api/config`);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the server did not serve ${url} in time:\n${server.log.join("")}`);
}

async function stop(server: Server) {
  if (server.failure || server.process.exitCode !== null) return;
  const exited = new Promise((resolve) => server.process.once("exit", resolve));
  server.process.kill("SIGTERM");
  const killed = setTimeout(() => server.process.kill("SIGKILL"), 5_000);
  await exited;
  clearTimeout(killed);
}

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address() as AddressInfo;
      probe.close(() => resolve(port));
    });
  });
}
