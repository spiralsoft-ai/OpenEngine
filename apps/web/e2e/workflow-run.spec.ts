import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const TASK = "Add a greeting file to the repository.";
const PULL_REQUEST = "https://github.com/acme/api/pull/7";
const FINDING = "greeting.txt is not covered by a test.";
const DECISION = "The finding can wait; the greeting is what was asked for.";

/** What the implementation waits for before writing anything.
 *
 *  A file the test creates rather than a sleep: the streaming assertion is
 *  about what is on screen *while* the step is running, and a race decided by a
 *  timer would fail in the direction that quietly hides a broken stream -- the
 *  step would simply have finished, and the finished transcript reads the same.
 *  The path is relative because the command runs in the run's own checkout,
 *  which is also what makes the path shown on the run page checkable. */
const RELEASE = "go";
const COMMAND = `until [ -f ${RELEASE} ]; do sleep 0.05; done; echo hello > greeting.txt`;

const SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    // The reviewer is asked about the implementation *and quoted the original
    // task*, so its prompt contains the implementation's own scenario word.
    // The first match wins, so the one only a reviewer can match goes first.
    {
      when: "Inspect the workspace",
      steps: [
        { type: "say", text: "Read the change; one thing to note." },
        {
          type: "tool",
          name: "add_comment",
          arguments: {
            pr_url: PULL_REQUEST,
            comment: FINDING,
            file: "greeting.txt",
            line: 1,
          },
        },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Reviewed the greeting.",
            outputs: { findings: FINDING },
          },
        },
        { type: "say", text: "Left the finding on the pull request." },
      ],
    },
    {
      when: "greeting",
      steps: [
        { type: "say", text: "Writing the greeting." },
        { type: "run", command: COMMAND, approval: false },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Added the greeting.",
            outputs: { pr_url: PULL_REQUEST },
          },
        },
        // Every scenario ends in something said, because a turn ends in the
        // agent's answer. The runtime usually cancels the CLI the moment it
        // accepts a terminal result, so this is often never reached -- but a
        // turn that does finish first is only assembled in the order it was
        // streamed when its last item is a message. Workaround for #105; see
        // `e2e/README.md` before removing it.
        { type: "say", text: "Wrote greeting.txt." },
      ],
    },
  ],
};

/** One step's card on the run page, by the name the read model gives it.
 *
 *  Exactly, because "Review" is also how "Human review" starts. */
function step(page: Page, name: string) {
  return page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

/** The Final outcome cell of the strip under the run heading. */
function outcome(page: Page) {
  return page.locator(".stat").filter({ hasText: "Final outcome" }).locator(".stat-value");
}

for (const runner of ["codex", "claude"]) {
  test(`a ${runner} workflow run provisions, streams, reviews, and is approved`, async ({
    page,
    engine,
  }, testInfo) => {
    engine.script(SCRIPT);

    await page.goto("/runs/new");
    await page.getByLabel("Repository").fill(engine.repository);
    await page.getByLabel("Implementation runner").selectOption(runner);
    await page.getByLabel("Task prompt").fill(TASK);
    await page.getByRole("button", { name: "Create workflow run" }).click();

    await expect(page).toHaveURL(/\/runs\/run-/);
    const runUrl = new URL(page.url()).pathname;

    // Provisioning: the run leaves the queue, the workspace stage is behind it,
    // and the checkout it names is a directory that exists.
    await expect(page.locator(".detail-title .chip")).toHaveText("implementing");
    await expect(page.locator(".stages .stage").first()).toHaveAttribute(
      "data-status",
      "completed",
    );
    const checkout = page.locator(".run-workspace .dock-path");
    await expect(checkout).toContainText("cd ");
    const workspace = ((await checkout.textContent()) ?? "").replace(/^cd\s*/, "").trim();
    expect(existsSync(workspace)).toBe(true);
    await shot(page, testInfo, "1 provisioned");

    // Streaming, asserted while the step is still running: a workflow
    // conversation is served by polling the durable transcript, and until the
    // step ends the page is shown nothing else -- so what is on screen here
    // arrived over that stream rather than with the page.
    await step(page, "Implementation")
      .getByRole("link", { name: "Open conversation" })
      .click();
    await expect(page).toHaveURL(/\/conversations\//);
    await expect(page.getByText("Writing the greeting.")).toBeVisible();
    await expect(page.locator(".tool-detail").first()).toHaveText(COMMAND);
    await shot(page, testInfo, "2 mid-step, streaming");

    // Let the command finish. The file the agent then writes is the evidence
    // that the path the run page named is the one it is working in.
    writeFileSync(path.join(workspace, RELEASE), "", "utf-8");
    await expect
      .poll(() => existsSync(path.join(workspace, "greeting.txt")))
      .toBe(true);

    // Advancing: `complete_step` carried the declared output, the review step
    // ran on the reviewer, and the run stops where a person has to decide.
    await page.goto(runUrl);
    await expect(page.locator(".detail-title .chip")).toHaveText(
      "awaiting human review",
    );
    await expect(step(page, "Implementation").locator(".step-outputs")).toContainText(
      PULL_REQUEST,
    );
    await expect(step(page, "Review").locator(".step-outputs")).toContainText(FINDING);
    await expect(page.locator(".callout-action")).toContainText("Action required");
    await shot(page, testInfo, "3 review reached");

    // The reviewer's finding left the process the way a real one would, through
    // `gh` -- which here records rather than comments on somebody's repository.
    expect(readFileSync(engine.ghLog, "utf-8")).toContain(FINDING);

    // The decision. Everything above this the runtime drives on its own; this
    // is the one transition no agent can make, so the browser is the only thing
    // that can finish the run.
    const pending = page.locator(".callout-action");
    await pending.getByLabel("Decision note").fill(DECISION);
    await pending.getByRole("button", { name: "Approve" }).click();

    await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
    await expect(outcome(page)).toHaveText("approved");
    await expect(page.locator(".callout")).toContainText(DECISION);
    await shot(page, testInfo, "4 approved");

    // Reloaded, because what matters is that the decision was persisted rather
    // than that the page it was submitted from redrew itself. Every stage is
    // behind the run now, the human one included.
    await page.reload();
    await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
    await expect(outcome(page)).toHaveText("approved");
    for (const name of ["Implementation", "Review", "Human review"])
      await expect(step(page, name).locator(".chip")).toHaveText("completed");
    const stages = page.locator(".stages .stage");
    await expect(stages).toHaveCount(4);
    for (let index = 0; index < 4; index += 1)
      await expect(stages.nth(index)).toHaveAttribute("data-status", "completed");
    await shot(page, testInfo, "5 approved, reloaded");
  });
}
