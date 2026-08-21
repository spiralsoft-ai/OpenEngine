import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiWorkflowRun } from "./api";
import { Sidebar } from "./sidebar";

/** The rail's chat list is assistant-ui's, and mounting the real runtime would
 *  test their thread store rather than our rail. One chat, standing in. */
vi.mock("@assistant-ui/react", () => {
  const thread = {
    remoteId: "thread-1",
    custom: { agentId: "coder", runner: "claude-code" },
    isRunning: false,
  };
  const Div = ({ children, ...props }: ComponentProps<"div">) => (
    <div {...props}>{children}</div>
  );
  const Button = ({ children, ...props }: ComponentProps<"button">) => (
    <button type="button" {...props}>
      {children}
    </button>
  );
  return {
    useAuiState: (select: (state: { threadListItem: typeof thread }) => unknown) =>
      select({ threadListItem: thread }),
    ThreadListPrimitive: {
      Root: Div,
      New: Button,
      Items: ({ archived, children }: { archived?: boolean; children: () => ReactNode }) =>
        archived ? null : children(),
    },
    ThreadListItemPrimitive: {
      Root: Div,
      Trigger: Button,
      Title: () => <>First chat</>,
      Archive: Button,
      Unarchive: Button,
    },
  };
});

const run: ApiWorkflowRun = {
  runId: "run-1",
  name: "First run",
  workflowId: "work-v1",
  workflowName: "Work",
  workflowVersion: "v1",
  taskId: "task-1",
  taskPrompt: "Do the work",
  repository: ".",
  repositoryContext: { repository: "." },
  phase: "running_agent",
  currentStepId: "implement",
  terminalOutcome: null,
  failureReason: "",
  steps: [
    {
      stepId: "implement",
      name: "Implementation",
      kind: "agent",
      status: "in_progress",
      outcome: null,
      summary: "",
      outputs: [],
      changesRequested: false,
      agentId: "agent",
      agentInstanceId: "instance",
      agentRunId: "agent-run",
      conversationId: "conversation",
      conversationUrl: "/runs/run-1/conversations/conversation",
      waiting: false,
    },
  ],
  pendingHumanReview: null,
  humanDecision: null,
};

function header(name: string) {
  return screen.getByRole("button", { name });
}

/** The part of a section a header controls, open or not, which is where that
 *  section's own buttons and items are. */
function body(name: string) {
  const id = header(name).getAttribute("aria-controls") as string;
  return document.getElementById(id) as HTMLElement;
}

describe("Sidebar", () => {
  it("keeps the three sections in one order and opens only the one asked for", () => {
    const { container } = render(<Sidebar runs={[run]} initialSection="workflows" />);

    const headers = [...container.querySelectorAll("[aria-expanded]")];
    expect(headers.map((element) => element.textContent)).toEqual([
      "Projects",
      "Workflows",
      "Chats",
    ]);
    expect(header("Workflows")).toHaveAttribute("aria-expanded", "true");
    expect(header("Projects")).toHaveAttribute("aria-expanded", "false");
    expect(header("Chats")).toHaveAttribute("aria-expanded", "false");
  });

  it("moves the open state to the clicked section and closes the others", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[run]} initialSection="workflows" />);

    await user.click(header("Chats"));

    expect(header("Chats")).toHaveAttribute("aria-expanded", "true");
    expect(header("Workflows")).toHaveAttribute("aria-expanded", "false");

    await user.click(header("Projects"));

    expect(header("Projects")).toHaveAttribute("aria-expanded", "true");
    expect(header("Chats")).toHaveAttribute("aria-expanded", "false");
    expect(header("Workflows")).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps a closed section mounted but out of reach", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[run]} initialSection="chats" />);

    expect(body("Workflows")).toHaveAttribute("inert");
    expect(within(body("Workflows")).getByRole("link", { name: "+ New workflow" })).toBeVisible();

    await user.click(header("Workflows"));

    expect(body("Workflows")).not.toHaveAttribute("inert");
    expect(body("Chats")).toHaveAttribute("inert");
  });

  it("puts each section's new button under its own header", () => {
    render(<Sidebar runs={[run]} initialSection="workflows" />);

    expect(within(body("Workflows")).getByRole("link", { name: "+ New workflow" })).toHaveAttribute(
      "href",
      "/runs/new",
    );
    expect(within(body("Chats")).getByRole("button", { name: "+ New chat" })).toBeInTheDocument();
    expect(within(body("Projects")).getByRole("link", { name: "Plan" })).toHaveAttribute(
      "href",
      "/plan",
    );
    expect(within(body("Projects")).getByText(/in progress/i)).toBeInTheDocument();
  });

  /** Planning is a conversation, and the button that starts one is the same
   *  accented control the other two sections lead with. */
  it("leads the projects section with the accented plan button", () => {
    render(<Sidebar runs={[run]} initialSection="projects" />);

    expect(within(body("Projects")).getByRole("link", { name: "Plan" })).toHaveClass(
      "rail-button",
      "rail-button-primary",
    );
  });

  it("lists runs with their conversations and marks the one on screen", () => {
    render(<Sidebar runs={[run]} initialSection="workflows" activeRunId="run-1" />);

    const entry = within(body("Workflows")).getByRole("link", { name: /First run/ });
    expect(entry).toHaveAttribute("href", "/runs/run-1");
    expect(entry).toHaveTextContent("Implementation · v1");
    expect(entry.closest(".rail-item")).toHaveAttribute("data-active", "true");
    expect(
      within(body("Workflows")).getByRole("link", { name: "Implementation conversation" }),
    ).toHaveAttribute("href", "/runs/run-1/conversations/conversation");
  });

  it("marks the open conversation rather than the run it belongs to", () => {
    render(
      <Sidebar
        runs={[run]}
        initialSection="workflows"
        activeRunId="run-1"
        activeConversationUrl="/runs/run-1/conversations/conversation"
      />,
    );

    const entry = within(body("Workflows")).getByRole("link", { name: /First run/ });
    expect(entry.closest(".rail-item")).not.toHaveAttribute("data-active");
    expect(
      within(body("Workflows")).getByRole("link", { name: "Implementation conversation" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("marks a workflow conversation that is waiting for input", () => {
    const waiting = {
      ...run,
      steps: [{ ...run.steps[0], waiting: true }],
    };
    render(<Sidebar runs={[waiting]} initialSection="workflows" />);

    const entry = within(body("Workflows")).getByRole("link", {
      name: "Implementation conversation Waiting for input",
    });
    expect(entry).toHaveTextContent("Implementation conversation ❔");
  });

  /** Switching the open conversation in place only means something beside a
   *  chat of its own; from anywhere else a chat is a link to its own page. */
  it("links chats away from a page that is not a standalone chat", () => {
    const { rerender } = render(<Sidebar runs={[]} initialSection="chats" linkChats />);

    expect(within(body("Chats")).getByRole("link", { name: "+ New chat" })).toHaveAttribute(
      "href",
      "/conversations",
    );
    expect(within(body("Chats")).getByRole("link", { name: /First chat/ })).toHaveAttribute(
      "href",
      "/conversations/thread-1",
    );

    rerender(<Sidebar runs={[]} initialSection="chats" />);

    expect(within(body("Chats")).getByRole("button", { name: "+ New chat" })).toBeInTheDocument();
    expect(within(body("Chats")).getByRole("button", { name: /First chat/ })).toBeInTheDocument();
  });
});
