import { expect, shot, test, type Script } from "./harness";

const SCRIPT: Script = {
  title: "Planning the greeting file",
  scenarios: [{ steps: [{ type: "say", text: "Here is what I would change." }] }],
};

test("the rail's plan button opens a new conversation with the planning agent", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/conversations");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("link", { name: "Plan" }).click();

  // The new conversation page, on the agent that plans rather than the one
  // that codes -- which is the whole of what the button settles.
  await expect(page).toHaveURL(/\/plan$/);
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  // Named by the field's own label, which a browser reads as the label text
  // followed by what the control offers -- hence the anchor rather than a
  // whole name, and hence not `getByLabel("Agent")`, which the composer's
  // "Message the agent" also answers to.
  await expect(page.getByRole("combobox", { name: /^Agent/ })).toHaveValue("planner");
  await shot(page, testInfo, "1 the plan page");

  await page.getByLabel("Message the agent").fill("How would you add a greeting file?");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("Here is what I would change.")).toBeVisible();
  await shot(page, testInfo, "2 the planner answers");

  const { threads } = await (await page.request.get("/api/threads")).json();
  expect(threads).toHaveLength(1);
  // The chat is an ordinary one, listed beside the others: what a plan starts
  // is a conversation, and only its agent is different.
  expect(threads[0].agentId).toBe("planner");
});
