import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const SEEDED_RUN = "Seeded navigation coverage";
const SEEDED_CHAT = "Seeded SQLite conversation";
const FRESH_CHAT_REPLY = "Fresh chat started without disturbing seeded history.";

function step(page: Page, name: string) {
  return page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

test.use({ seededDatabase: true });

test("seeded SQLite history stays navigable while new chats and workflows start", async ({
  page,
  engine,
}, testInfo) => {
  // The process starts with the database already populated. Begin at the list
  // page, then follow the same links a person uses instead of addressing each
  // API or detail route directly.
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Workflow runs" })).toBeVisible();
  const seededRunCard = page
    .locator(".cards")
    .getByRole("link", { name: new RegExp(SEEDED_RUN) });
  await expect(seededRunCard).toBeVisible();
  await shot(page, testInfo, "1 seeded workflow list");

  await seededRunCard.click();
  await expect(page.getByRole("heading", { name: SEEDED_RUN })).toBeVisible();
  await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
  await expect(step(page, "Implementation")).toContainText("Preserved every browser route.");
  await expect(step(page, "Review")).toContainText("No navigation regressions found.");
  await expect(page.getByRole("heading", { name: "approved" })).toBeVisible();
  await shot(page, testInfo, "2 seeded workflow detail");

  await step(page, "Implementation")
    .getByRole("link", { name: "Open conversation" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Seeded implementation conversation" }),
  ).toBeVisible();
  await expect(
    page.getByText("Preserve browser navigation and durable conversation history."),
  ).toBeVisible();
  await expect(
    page.getByText("I preserved the routes and added durable history coverage."),
  ).toBeVisible();
  await shot(page, testInfo, "3 seeded implementation history");

  await page.getByRole("link", { name: /Back to run run-seeded-history/ }).click();
  await step(page, "Review").getByRole("link", { name: "Open conversation" }).click();
  await expect(
    page.getByRole("heading", { name: "Seeded review conversation" }),
  ).toBeVisible();
  await expect(
    page.getByText("I found no navigation regressions in the seeded workflow."),
  ).toBeVisible();
  await shot(page, testInfo, "4 seeded review history");

  // Workflow transcripts and ordinary chats share SQLite but have different
  // routes. Cross that boundary through the rail and verify both turns of the
  // pre-existing standalone conversation survived the cold start.
  await page.getByRole("button", { name: "Chats", exact: true }).click();
  await page.getByRole("link", { name: new RegExp(SEEDED_CHAT) }).click();
  await expect(page.getByRole("heading", { name: SEEDED_CHAT })).toBeVisible();
  await expect(page.getByText("What survives when the web process restarts?")).toBeVisible();
  await expect(
    page.getByText("The SQLite-backed conversation history survives."),
  ).toBeVisible();
  await expect(page.getByText("Can I still navigate back to this answer?")).toBeVisible();
  await expect(
    page.getByText("Yes. This second turn proves the complete history loaded."),
  ).toBeVisible();
  await shot(page, testInfo, "5 seeded standalone chat history");

  // Existing rows must not make the create path behave like a restore path.
  engine.script({
    title: "Fresh chat beside history",
    scenarios: [
      {
        when: "fresh chat",
        steps: [{ type: "say", text: FRESH_CHAT_REPLY }],
      },
    ],
  });
  await page.getByRole("button", { name: "+ New chat", exact: true }).click();
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  await page.getByLabel("Message the agent").fill("Start a fresh chat beside history.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(FRESH_CHAT_REPLY)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Fresh chat beside history" })).toBeVisible();
  await shot(page, testInfo, "6 fresh chat beside seeded history");

  const pullRequest = "https://github.com/acme/engine/pull/42";
  const workflowScript: Script = {
    title: "Fresh workflow beside history",
    scenarios: [
      {
        when: "Inspect the workspace",
        steps: [
          { type: "say", text: "Reviewing the fresh workflow." },
          {
            type: "tool",
            name: "add_comment",
            arguments: {
              pr_url: pullRequest,
              comment: "Fresh workflow reached review from the seeded database.",
            },
          },
          {
            type: "tool",
            name: "complete_step",
            arguments: {
              outcome: "success",
              summary: "Reviewed the fresh workflow.",
              outputs: { findings: "No findings." },
            },
          },
          { type: "say", text: "The fresh workflow is ready for human review." },
        ],
      },
      {
        when: "fresh workflow",
        steps: [
          { type: "say", text: "Starting a workflow beside the seeded run." },
          {
            type: "tool",
            name: "complete_step",
            arguments: {
              outcome: "success",
              summary: "Started from the populated SQLite database.",
              outputs: { pr_url: pullRequest },
            },
          },
          { type: "say", text: "The fresh workflow implementation completed." },
        ],
      },
    ],
  };
  engine.script(workflowScript);
  await page.goto("/runs/new");
  await expect(page.getByRole("heading", { name: "Start a workflow" })).toBeVisible();
  await page.getByLabel("Repository").fill(engine.repository);
  await page.getByLabel("Task prompt").fill("Start a fresh workflow beside history.");
  await page.getByRole("button", { name: "Create workflow run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  await expect(page.locator(".detail-title .chip")).toHaveText("Human review");
  await expect(step(page, "Implementation")).toContainText(
    "Started from the populated SQLite database.",
  );
  await shot(page, testInfo, "7 fresh workflow beside seeded history");

  // Creating new records did not replace the old ones: the original run is
  // still reachable from the list after both new execution paths completed.
  await page.getByRole("link", { name: "All workflow runs", exact: true }).click();
  await expect(
    page.locator(".cards").getByRole("link", { name: new RegExp(SEEDED_RUN) }),
  ).toBeVisible();
});
