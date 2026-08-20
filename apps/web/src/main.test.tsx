/** What the rail's Project Manager button opens.
 *
 *  The link is half the feature and the page it lands on is the other half.
 *  What settles who answers is not the heading but the defaults the chat
 *  runtime creates a conversation from, so that is what this reads: a route
 *  printing "Project manager" while starting a `coder` chat fails here. */

import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./main";

const stub = vi.hoisted(() => ({
  config: {
    agents: [
      { id: "coder", description: "Reads code.", instructions: "" },
      { id: "project-manager", description: "Plans a project.", instructions: "" },
    ],
    runners: [{ id: "codex", implementation: "codex" }],
    defaultAgent: "coder",
    defaultRunner: "codex",
    workflowRunners: ["codex"],
    defaultWorkflowRunner: "codex",
  },
  defaults: undefined as { agentId: string; runner: string } | undefined,
  rail: undefined as { initialSection: string; activeView?: string } | undefined,
}));

/** No conversation is open on a page reached by URL, which is what makes the
 *  header the new-chat one rather than an open chat's. */
vi.mock("@assistant-ui/react", () => ({
  useAui: () => ({ threads: { reload: async () => {} } }),
  useAuiState: () => undefined,
}));

vi.mock("./api", () => ({
  api: async () => stub.config,
  setThreadRunner: async () => stub.config,
}));

vi.mock("./chat", () => ({
  ChatThread: () => <div data-testid="chat-thread" />,
  ConversationStats: () => null,
}));

vi.mock("./runs", () => ({
  useRuns: () => ({ runs: [], error: "" }),
  RunsPage: () => null,
  NewWorkflowPage: () => null,
  RunDetailPage: () => null,
}));

vi.mock("./sidebar", () => ({
  Sidebar: (props: { initialSection: string; activeView?: string }) => {
    stub.rail = props;
    return <aside data-testid="rail" />;
  },
}));

vi.mock("./runtime", () => ({
  EngineRuntimeProvider: ({
    defaults,
    children,
  }: {
    defaults: { agentId: string; runner: string };
    children: ReactNode;
  }) => {
    stub.defaults = defaults;
    return <>{children}</>;
  },
}));

async function visit(path: string) {
  window.history.replaceState({}, "", path);
  render(<App />);
  await screen.findByTestId("rail");
}

describe("the project manager page", () => {
  beforeEach(() => {
    stub.defaults = undefined;
    stub.rail = undefined;
  });

  it("starts its conversation on the project manager, not the new-chat default", async () => {
    await visit("/projects/manager");

    expect(stub.defaults).toEqual({ agentId: "project-manager", runner: "codex" });
  });

  it("is the new-chat screen, with the one agent stated rather than offered", async () => {
    await visit("/projects/manager");

    expect(screen.getByRole("heading", { name: "Project manager" })).toBeInTheDocument();
    expect(screen.getByTestId("chat-thread")).toBeInTheDocument();
    // The runner is still the reader's to pick; the agent is the page's.
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
    expect(screen.getByText("project-manager")).toBeInTheDocument();
  });

  it("opens the rail on the section the button was in, and marks it", async () => {
    await visit("/projects/manager");

    expect(stub.rail).toMatchObject({
      initialSection: "projects",
      activeView: "project-manager",
    });
  });

  it("leaves every other chat on the configured default", async () => {
    await visit("/conversations");

    expect(stub.defaults).toEqual({ agentId: "coder", runner: "codex" });
    expect(stub.rail).toMatchObject({ initialSection: "chats", activeView: undefined });
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
  });
});
