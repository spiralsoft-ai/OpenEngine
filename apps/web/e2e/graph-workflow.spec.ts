/** The same WorkOrder, run by the graph engine instead of the step executor.
 *
 *  `workflow-run.spec.ts` is one long test, because a step WorkOrder does all
 *  of this and a single spec can walk through it. This one is the same journey
 *  split into the states it passes through, one test each, because a `[BETA]`
 *  WorkOrder does *not* do all of it yet: split, a run reports every gap it
 *  has, where one long test would report only the first.
 *
 *  That is what this file is for. It is expected to fail, its CI job says so,
 *  and each red test names one thing the graph WorkOrder cannot do that the
 *  step one can. Turning them green is the work; deleting them is not.
 *
 *  Everything here is the real thing except the model: a real server, the real
 *  graph engine, real LangGraph, real checkpoint files, a real worktree, and
 *  agents reached over real ACP -- answered by `tests/provider_fakes.py`
 *  instead of by codex or claude. */

import { existsSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

/** What the dropdown calls the graph, and what the runner-less form promises. */
const WORKFLOW = "[BETA] Implementation review (codex)";
const TASK = "Add a greeting file to the repository.";
const GREETING = "greeting.txt";
const IMPLEMENTED = "Wrote the greeting.";
const REVIEWED = "Read the change; greeting.txt is not covered by a test.";

const STEER = "Also write a licence file.";
const STEERED = "Wrote the licence.";

/** The same journey, with the agent stopping to ask before it writes.
 *
 *  Which is the state a conversation has to be readable and answerable in: the
 *  node is in flight, holding its turn, and the person it is waiting on is the
 *  one reading the page. */
const ASKING_SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    { when: "Review the implementation", steps: [{ type: "say", text: REVIEWED }] },
    { when: STEER, steps: [{ type: "say", text: STEERED }] },
    {
      when: "Implement the requested change",
      steps: [
        { type: "say", text: "Writing the greeting." },
        { type: "run", command: `echo hello > ${GREETING}`, approval: true },
        { type: "say", text: IMPLEMENTED },
      ],
    },
  ],
};

const SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    // The reviewer is asked about the implementation *and quoted the original
    // task*, so its prompt contains the implementation's own scenario word.
    // The first match wins, so the one only a reviewer can match goes first.
    { when: "Review the implementation", steps: [{ type: "say", text: REVIEWED }] },
    {
      when: "Implement the requested change",
      steps: [
        { type: "say", text: "Writing the greeting." },
        // No approval: what this spec is about is the journey, and an agent
        // stopping to ask permission is a state of its own -- see the last
        // test in this file.
        { type: "run", command: `echo hello > ${GREETING}`, approval: false },
        { type: "say", text: IMPLEMENTED },
      ],
    },
  ],
};

/** Create one `[BETA]` WorkOrder and land on its page. */
async function create(page: Page, repository: string): Promise<string> {
  await page.goto("/runs/new");
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  await page.getByLabel("Repository").fill(repository);
  await page.getByLabel("Task prompt").fill(TASK);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  return new URL(page.url()).pathname;
}

/** What the graph engine says about a run, which is the truth the page lags. */
async function graphRun(page: Page, runUrl: string) {
  const runId = runUrl.split("/").pop() ?? "";
  const response = await page.request.get(`/graph/api/runs/${runId}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

test("@beta a graph workflow is offered, and does not ask for a runner", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/runs/new");

  await expect(
    page.getByLabel("Workflow definition").getByRole("option", { name: WORKFLOW }),
  ).toHaveCount(1);
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  // The graph names the agent it runs, so there is nothing to choose.
  await expect(page.getByLabel("Implementation runner")).toHaveCount(0);
  await shot(page, testInfo, "1 the beta choice");
});

test("@beta a graph WorkOrder provisions a checkout and runs its agents", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);

  // Asked of the graph engine, because the WorkOrder page cannot show a graph
  // run's position yet. The checkout it names is a directory that exists, and
  // the file the agent wrote is in it.
  await expect
    .poll(async () => (await graphRun(page, runUrl)).values?.implementation ?? "", {
      timeout: 60_000,
    })
    .toContain(IMPLEMENTED);
  const run = await graphRun(page, runUrl);
  const workspace = String(run.values?.workspace ?? "");
  expect(existsSync(workspace)).toBe(true);
  expect(existsSync(path.join(workspace, GREETING))).toBe(true);
  await shot(page, testInfo, "2 implemented");
});

test("@beta the WorkOrder page shows a graph run's stages", async ({ page, engine }) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  // The four stages a step run of the same workflow shows on this page.
  await expect(page.locator(".stages .stage")).toHaveText([
    "Workspace",
    "Implementation",
    "Review",
    "Human review",
  ]);
});

test("@beta the checkout a graph run works in is on its WorkOrder page", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  const checkout = page.locator(".run-workspace .dock-path");
  await expect(checkout).toContainText("cd ");
});

/** Open the implementation node's conversation from the WorkOrder page. */
async function openConversation(page: Page, runUrl: string): Promise<void> {
  await page.goto(runUrl);
  await page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name: "Implementation", exact: true }) })
    .getByRole("link", { name: "Open conversation" })
    .click();
  await expect(page).toHaveURL(/\/conversations\//);
}

test("@beta an agent's conversation is readable from the WorkOrder page", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await openConversation(page, runUrl);

  // Both halves of it, drawn as the chat draws a chat: what the node was asked
  // is the reader's turn, what it said is the agent's, and the command it ran
  // is a folded row in between rather than a paragraph of its own.
  await expect(page.locator(".message-user").first()).toContainText(TASK);
  await expect(page.getByText("Writing the greeting.")).toBeVisible();
  await expect(page.locator(".message-assistant .tool").first()).toContainText(
    `echo hello > ${GREETING}`,
  );
  await shot(page, testInfo, "4 the conversation");
});

test("@beta an agent waiting on permission is answered in its conversation", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(ASKING_SCRIPT);

  const runUrl = await create(page, engine.repository);
  await expect
    .poll(async () => (await graphRun(page, runUrl)).pendingApprovals?.length ?? 0, {
      timeout: 60_000,
    })
    .toBe(1);
  await openConversation(page, runUrl);

  // The question, where the command it is about is: the run is stopped here,
  // and this is the page the person it is stopped on is reading.
  const card = page.locator(".approval-pending");
  await expect(card).toContainText(`echo hello > ${GREETING}`);
  await shot(page, testInfo, "5 waiting on permission");

  // Steering, while it waits. The agent is holding its turn, so this is a
  // message into the conversation rather than a new one.
  await page.getByLabel("Message the agent").fill(STEER);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(STEER)).toBeVisible();

  await card.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByText(STEERED)).toBeVisible({ timeout: 60_000 });
});

test("@beta a graph run waiting on a person says so, and can be answered", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);

  // The graph engine is the one that knows: the run reaches its human-review
  // node and raises an approval there.
  await expect
    .poll(async () => (await graphRun(page, runUrl)).pendingApprovals?.length ?? 0, {
      timeout: 60_000,
    })
    .toBe(1);
  await page.goto(runUrl);
  await shot(page, testInfo, "3 waiting for a person");

  // What a person is shown, and what they press. Both are what the step
  // WorkOrder does at exactly this point.
  await expect(page.locator(".callout-action")).toContainText("Action required");
  await page.getByLabel("Decision note").fill("Ship it.");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
});
