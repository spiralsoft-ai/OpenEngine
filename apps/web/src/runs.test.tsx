import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiWorkflowRun, EngineConfig } from "./api";
import {
  conversationCount,
  NewWorkflowPage,
  phaseAccent,
  phaseLabel,
  RunDetailPage,
  RunsPage,
  useRuns,
} from "./runs";

const config: EngineConfig = {
  agents: [],
  runners: [],
  defaultAgent: "agent",
  defaultRunner: "runner",
  workflowRunners: ["codex"],
  defaultWorkflowRunner: "codex",
};

function run(overrides: Partial<ApiWorkflowRun> = {}): ApiWorkflowRun {
  return {
    runId: "run-1",
    name: "First run",
    workflowId: "implementation-review-v1",
    workflowName: "Implementation review",
    workflowVersion: "v1",
    taskId: "task-1",
    taskPrompt: "Do the work",
    repository: ".",
    repositoryContext: { repository: "." },
    phase: "implementing",
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
        conversationUrl: "/conversations/conversation",
        waiting: false,
      },
      {
        stepId: "review",
        name: "Review",
        kind: "human",
        status: "pending",
        outcome: null,
        summary: "",
        outputs: [],
        changesRequested: false,
        agentId: null,
        agentInstanceId: null,
        agentRunId: null,
        conversationId: null,
        conversationUrl: null,
        waiting: false,
      },
    ],
    pendingHumanReview: null,
    humanDecision: null,
    ...overrides,
  };
}

function json(value: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function stubPageApi(runs: ApiWorkflowRun[] = []) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/runs" && init?.method === "POST")
      return json(run({ runId: "created-run" }));
    if (path === "/api/runs") return json({ runs });
    return json({ error: "not found" }, { status: 404 });
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("run display helpers", () => {
  it("formats phases and accents their significant states", () => {
    expect(phaseLabel("awaiting_human_review")).toBe("awaiting human review");
    expect(phaseAccent("failed")).toBe("flame");
    expect(phaseAccent("pending")).toBe("quiet");
    expect(phaseAccent("preparing_workspace")).toBe("quiet");
    expect(phaseAccent("implementing")).toBeUndefined();
  });

  it("counts only steps with conversations", () => {
    expect(conversationCount(run())).toBe(1);
    expect(conversationCount(run({ steps: [] }))).toBe(0);
  });
});

describe("NewWorkflowPage", () => {
  it("restores a prompt after unmounting and remounting", async () => {
    vi.stubGlobal("fetch", stubPageApi());
    const user = userEvent.setup();
    const first = render(<NewWorkflowPage config={config} />);
    const prompt = screen.getByRole("textbox", { name: "Task prompt" });

    await user.type(prompt, "Keep this draft");
    await waitFor(() =>
      expect(window.localStorage.getItem("engine.workflowDraft")).toBe("Keep this draft"),
    );
    first.unmount();
    render(<NewWorkflowPage config={config} />);

    expect(screen.getByRole("textbox", { name: "Task prompt" })).toHaveValue(
      "Keep this draft",
    );
  });

  it("clears the saved prompt after creating a run", async () => {
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<NewWorkflowPage config={config} />);
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    const submit = screen.getByRole("button", { name: "Create workflow run" });
    await waitFor(() => expect(submit).toBeEnabled());

    await user.click(submit);

    await waitFor(() => expect(window.localStorage.getItem("engine.workflowDraft")).toBeNull());
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("useRuns", () => {
  it("reads the runs the shell hands to both the page and the rail", async () => {
    vi.stubGlobal("fetch", stubPageApi([run()]));

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    expect(result.current.runs[0].runId).toBe("run-1");
    expect(result.current.error).toBe("");
  });

  it("reports a failure instead of an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ error: "runs unavailable" }, { status: 500 })),
    );

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.error).toBe("runs unavailable"));
    expect(result.current.runs).toEqual([]);
  });

  it("refreshes terminal runs so reactivated waiting conversations appear", async () => {
    vi.useFakeTimers();
    const terminal = run({
      phase: "succeeded",
      currentStepId: null,
      terminalOutcome: "approved",
      steps: run().steps.map((step) => ({ ...step, status: "completed" })),
    });
    const active = run();
    const waiting = run({ steps: [{ ...active.steps[0], waiting: true }] });
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ runs: [terminal] }))
      .mockResolvedValue(json({ runs: [waiting] }));
    vi.stubGlobal("fetch", fetch);

    const { result } = renderHook(() => useRuns());
    await act(async () => {});
    expect(result.current.runs[0].phase).toBe("succeeded");
    expect(result.current.runs[0].steps[0].waiting).toBe(false);

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(result.current.runs[0].phase).toBe("implementing");
    expect(result.current.runs[0].steps[0].waiting).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps refreshing after a transient load failure", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("runs unavailable"))
      .mockResolvedValue(json({ runs: [run()] }));
    vi.stubGlobal("fetch", fetch);

    const { result } = renderHook(() => useRuns());
    await act(async () => {});
    expect(result.current.error).toBe("runs unavailable");

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(result.current.runs).toHaveLength(1);
    expect(result.current.error).toBe("");
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("RunsPage", () => {
  it("renders its empty state", () => {
    render(<RunsPage runs={[]} error="" />);

    expect(screen.getByRole("heading", { name: "No workflow runs yet." })).toBeVisible();
  });

  it("reports a load failure over the empty state", () => {
    render(<RunsPage runs={[]} error="runs unavailable" />);

    expect(screen.getByText(/runs unavailable/)).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "No workflow runs yet." }),
    ).not.toBeInTheDocument();
  });

  it("renders stage and chat counts and offers only phases that exist", async () => {
    const runs = [
      run(),
      run({
        runId: "run-2",
        name: "Second run",
        phase: "failed",
        currentStepId: null,
        terminalOutcome: "rejected",
        steps: [],
      }),
    ];
    const user = userEvent.setup();
    const { container } = render(<RunsPage runs={runs} error="" />);

    await screen.findByRole("heading", { name: "First run" });
    const firstCard = container.querySelector('.cards a[href="/runs/run-1"]');
    expect(firstCard).not.toBeNull();
    expect(within(firstCard as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(firstCard as HTMLElement).getByText("1")).toBeInTheDocument();
    expect(within(firstCard as HTMLElement).getByText("Implementation")).toBeInTheDocument();

    const filters = screen.getByRole("group", { name: "Filter runs by phase" });
    expect(within(filters).getByRole("button", { name: "implementing" })).toBeInTheDocument();
    expect(within(filters).getByRole("button", { name: "failed" })).toBeInTheDocument();
    expect(within(filters).queryByRole("button", { name: "pending" })).not.toBeInTheDocument();

    await user.click(within(filters).getByRole("button", { name: "failed" }));
    expect(container.querySelectorAll(".cards .card")).toHaveLength(1);
    expect(screen.getByText("1 of 2 shown")).toBeInTheDocument();
  });
});

describe("RunDetailPage", () => {
  const awaiting = run({
    phase: "awaiting_human_review",
    currentStepId: "review",
    steps: run().steps.map((step, index) => ({
      ...step,
      status: index ? "action_required" : "completed",
    })),
    pendingHumanReview: {
      stepId: "review",
      title: "Review implementation for task-1",
      summary: "Added the greeting.",
    },
  });

  it("records a human approval and shows the decision it caused", async () => {
    const approved = run({
      phase: "succeeded",
      currentStepId: null,
      terminalOutcome: "approved",
      steps: run().steps.map((step) => ({ ...step, status: "completed" })),
      humanDecision: {
        stepId: "review",
        approved: true,
        outcome: "approved",
        summary: "Ship it",
      },
    });
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1/human-review" && init?.method === "POST")
        return json(approved);
      if (path === "/api/runs/run-1") return json(awaiting);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();

    render(<RunDetailPage runId="run-1" />);
    await user.type(
      await screen.findByRole("textbox", { name: "Decision note" }),
      "Ship it",
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Final human decision")).toBeVisible();
    expect(screen.getByRole("heading", { name: "approved" })).toBeVisible();
    expect(screen.getByText("Ship it")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1/human-review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: true, summary: "Ship it" }),
      }),
    );
  });

  it("offers rejection, and keeps the decision open when it is refused", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1/human-review" && init?.method === "POST")
        return json({ error: "run is not awaiting human review" }, { status: 409 });
      if (path === "/api/runs/run-1") return json(awaiting);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();

    render(<RunDetailPage runId="run-1" />);
    await user.click(await screen.findByRole("button", { name: "Reject" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "run is not awaiting human review",
    );
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1/human-review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: false, summary: "" }),
      }),
    );
  });

  it("offers the workflow checkout's detach operation", async () => {
    const terminal = run({
      phase: "succeeded",
      currentStepId: null,
      terminalOutcome: "approved",
    });
    const attached = {
      id: "instance",
      title: "Implementation",
      archived: false,
      agentId: "agent",
      runner: "codex",
      workspaceRoot: "/worktrees/ws-1",
      workspaceRef: "engine/ws-1",
      workspaceAttached: true,
    };
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(terminal);
      if (path === "/api/threads/instance/workspace" && init?.method === "DELETE")
        return json({
          ...attached,
          workspaceRoot: undefined,
          workspaceAttached: false,
        });
      if (path === "/api/threads/instance") return json(attached);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText("cd /worktrees/ws-1")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Detach" }));

    expect(await screen.findByText("git checkout engine/ws-1")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reattach" })).toBeEnabled();
    expect(fetch).toHaveBeenCalledWith(
      "/api/threads/instance/workspace",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
