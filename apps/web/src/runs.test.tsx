import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject, ApiWorkflowRun, EngineConfig } from "./api";
import {
  conversationCount,
  NewWorkflowPage,
  phaseAccent,
  phaseLabel,
  RunDetailPage,
  RunsPage,
  runStatusLabel,
  useRuns,
} from "./runs";

const config: EngineConfig = {
  agents: [],
  runners: [],
  defaultAgent: "agent",
  planAgent: "planner",
  defaultRunner: "runner",
  workflowRunners: ["codex"],
  defaultWorkflowRunner: "codex",
  workflows: [
    { id: "work-v1", name: "Work", version: "v1", kind: "steps" },
    { id: "release-v2", name: "Release", version: "v2", kind: "steps" },
  ],
};
/** The same deployment, with a graph workflow beside the step ones. */
const withBeta: EngineConfig = {
  ...config,
  workflows: [
    ...config.workflows,
    {
      id: "implementation-review-codex",
      name: "[BETA] Implementation review (codex)",
      version: "",
      kind: "graph",
    },
  ],
};
const project: ApiProject = {
  projectId: "project-1",
  name: "Engine roadmap",
  archived: false,
};
const milestone: ApiMilestone = {
  milestoneId: "milestone-foundation",
  name: "Foundation",
  description: "Build the shared model.",
  dependencies: [],
  workstreams: [
    { workstreamId: "workstream-data", name: "Data model", scope: "Persist it." },
  ],
};

function run(overrides: Partial<ApiWorkflowRun> = {}): ApiWorkflowRun {
  return {
    runId: "run-1",
    name: "First run",
    workflowId: "work-v1",
    workflowName: "Work",
    workflowVersion: "v1",
    taskId: "task-1",
    workstreamId: null,
    milestoneId: null,
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
    expect(phaseAccent("running_agent")).toBeUndefined();
  });

  it("counts only steps with conversations", () => {
    expect(conversationCount(run())).toBe(1);
    expect(conversationCount(run({ steps: [] }))).toBe(0);
  });

  it("labels active runs with their current workflow step", () => {
    expect(runStatusLabel(run())).toBe("Implementation");
    expect(runStatusLabel(run({ phase: "succeeded" }))).toBe("succeeded");
  });
});

describe("NewWorkflowPage", () => {
  it("offers every configured workflow definition", () => {
    render(<NewWorkflowPage config={config} />);

    expect(
      screen.getByText(
        "Create one WorkOrder that keeps its stages, agent conversations, outputs, and final human decision together.",
      ),
    ).toBeVisible();
    const selector = screen.getByRole("combobox", { name: "Workflow definition" });
    expect(within(selector).getByRole("option", { name: "Release · v2" })).toHaveValue(
      "release-v2",
    );
  });

  it("offers a beta workflow by its name alone, with no version to show", async () => {
    // A [BETA] entry is a graph workflow. It has no version, and "name · "
    // with nothing after it reads like something failed to load.
    render(<NewWorkflowPage config={withBeta} />);

    const selector = screen.getByRole("combobox", { name: "Workflow definition" });
    expect(
      within(selector).getByRole("option", { name: "[BETA] Implementation review (codex)" }),
    ).toHaveValue("implementation-review-codex");
  });

  it("does not ask which runner to use for a workflow that names its own agent", async () => {
    // The graph decides which agent runs it -- there is one entry per agent --
    // so the server reads no runner for one. A field that appears to choose
    // something and is then ignored is worse than no field.
    const user = userEvent.setup();
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(<NewWorkflowPage config={withBeta} />);
    expect(screen.getByRole("combobox", { name: "Implementation runner" })).toBeVisible();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Workflow definition" }),
      "implementation-review-codex",
    );

    expect(
      screen.queryByRole("combobox", { name: "Implementation runner" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/runs the agent named in its own definition/),
    ).toBeVisible();

    // And nothing is sent in its place: the WorkOrder carries the graph, the
    // prompt and the repository, which is all a graph is given.
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    await user.click(screen.getByRole("button", { name: "Create WorkOrder" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/runs", expect.anything()));
    const request = fetch.mock.calls.find(([url]) => url === "/api/runs")?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).not.toHaveProperty("runner");
  });

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
    const submit = screen.getByRole("button", { name: "Create WorkOrder" });
    await waitFor(() => expect(submit).toBeEnabled());

    await user.click(submit);

    await waitFor(() => expect(window.localStorage.getItem("engine.workflowDraft")).toBeNull());
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("creates a milestone task with an optional workstream", async () => {
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<NewWorkflowPage config={config} project={project} milestone={milestone} />);

    expect(
      screen.getByRole("option", { name: "No workstream — milestone task" }),
    ).toHaveValue("");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Workstream (optional)" }),
      "workstream-data",
    );
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Persist it");
    await user.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/runs", expect.anything()));
    const request = fetch.mock.calls.find(([url]) => url === "/api/runs")?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toMatchObject({
      milestoneId: "milestone-foundation",
      workstreamId: "workstream-data",
    });
  });
});

describe("useRuns", () => {
  it("reads the runs the shell hands to both the page and the rail", async () => {
    vi.stubGlobal("fetch", stubPageApi([run()]));

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    expect(result.current.runs[0].runId).toBe("run-1");
    expect(result.current.error).toBe("");
    expect(result.current.loaded).toBe(true);
  });

  it("reports a failure instead of an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ error: "runs unavailable" }, { status: 500 })),
    );

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.error).toBe("runs unavailable"));
    expect(result.current.runs).toEqual([]);
    expect(result.current.loaded).toBe(false);
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

    expect(result.current.runs[0].phase).toBe("running_agent");
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
    expect(result.current.loaded).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  /** Nothing puts a deleted run back, so the click is asked about first and
   *  the row leaves on the answer rather than a poll later. */
  it("deletes a run once the reader confirms, and drops its row at once", async () => {
    const fetch = stubPageApi([run()]);
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    const { result } = renderHook(() => useRuns());
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    await act(async () => result.current.remove(result.current.runs[0]));

    expect(window.confirm).toHaveBeenCalledWith(
      "Delete First run? This cannot be undone.",
    );
    expect(result.current.runs).toEqual([]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("keeps the run when the reader says no", async () => {
    const fetch = stubPageApi([run()]);
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(false));

    const { result } = renderHook(() => useRuns());
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    await act(async () => result.current.remove(result.current.runs[0]));

    expect(result.current.runs).toHaveLength(1);
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/runs/run-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});

describe("RunsPage", () => {
  it("renders its empty state", () => {
    render(<RunsPage runs={[]} error="" />);

    expect(screen.getByRole("heading", { name: "No WorkOrders yet." })).toBeVisible();
  });

  it("reports a load failure over the empty state", () => {
    render(<RunsPage runs={[]} error="runs unavailable" />);

    expect(screen.getByText(/runs unavailable/)).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "No WorkOrders yet." }),
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
    expect(within(firstCard as HTMLElement).getAllByText("Implementation")).toHaveLength(2);

    const filters = screen.getByRole("group", { name: "Filter WorkOrders by phase" });
    expect(within(filters).getByRole("button", { name: "running agent" })).toBeInTheDocument();
    expect(within(filters).getByRole("button", { name: "failed" })).toBeInTheDocument();
    expect(within(filters).queryByRole("button", { name: "pending" })).not.toBeInTheDocument();

    await user.click(within(filters).getByRole("button", { name: "failed" }));
    expect(container.querySelectorAll(".cards .card")).toHaveLength(1);
    expect(screen.getByText("1 of 2 shown")).toBeInTheDocument();
  });
});

describe("RunDetailPage", () => {
  it("says where to look for a beta WorkOrder that has no stages here", async () => {
    // A graph WorkOrder has no steps to draw, because a graph is not made of
    // them. Without a word of explanation the page reads as one that never
    // started.
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "Implementation review (codex)",
      workflowVersion: "",
      currentStepId: null,
      steps: [],
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1")
        return json({
          runId: "run-1",
          graphId: graphRun.workflowId,
          status: "running",
          activeExecutions: [],
          nextNodes: [],
          values: {},
          pendingApprovals: [],
          error: "",
        });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`)
        return json({ graphId: graphRun.workflowId, nodes: [] });
      if (path === "/api/runs/run-1/graph-events") return json({ events: [] });
      return json({ error: "not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText("Beta workflow")).toBeVisible();
    expect(screen.getByText("Implementation review (codex)")).toBeVisible();
  });

  it("opens a beta conversation before its first transcript arrives", async () => {
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "Implementation review (codex)",
      workflowVersion: "",
      currentStepId: null,
      steps: [],
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1")
        return json({
          runId: "run-1",
          graphId: graphRun.workflowId,
          status: "running",
          activeExecutions: [{ executionId: "execution-1", nodeId: "implementation" }],
          nextNodes: ["implementation"],
          values: {},
          pendingApprovals: [],
          error: "",
        });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`)
        return json({
          graphId: graphRun.workflowId,
          nodes: [{ nodeId: "implementation", name: "Implementation", kind: "agent" }],
        });
      if (path === "/api/runs/run-1/graph-events")
        return json({
          events: [{
            sequence: 4,
            type: "conversation.started",
            nodeId: "implementation",
            payload: { agent: "codex", sessionId: "session-1", resumed: false },
          }],
        });
      return json({ error: "not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    const link = await screen.findByRole("link", { name: /Open conversation/ });
    expect(link).toHaveAttribute(
      "href",
      "/runs/run-1/conversations/graph--implementation",
    );
    expect(screen.queryByText("Conversation not started")).not.toBeInTheDocument();
  });

  it("renders steps from an arbitrary workflow definition", async () => {
    const generic = run({
      workflowId: "release-v2",
      workflowName: "Release",
      workflowVersion: "v2",
      phase: "succeeded",
      currentStepId: "publish",
      terminalOutcome: "succeeded",
      steps: [
        {
          ...run().steps[0],
          stepId: "prepare",
          name: "Prepare release",
          status: "completed",
          summary: "Prepared artifacts.",
        },
        {
          ...run().steps[1],
          stepId: "publish",
          name: "Publish release",
          status: "completed",
          outcome: "approved",
        },
      ],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(generic)));

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByRole("heading", { name: "Prepare release" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Publish release" })).toBeVisible();
    expect(screen.getByText("Prepared artifacts.")).toBeVisible();
  });

  it("ends the run with the decision it stopped for", async () => {
    const completed = run().steps.map((step) => ({ ...step, status: "completed" }));
    const awaiting = run({
      phase: "awaiting_human_review",
      currentStepId: "human-review",
      steps: completed,
      pendingHumanReview: {
        stepId: "human-review",
        title: "Review implementation for task-1",
        summary: "Implementation: done",
        prUrl: null,
      },
    });
    const decided = run({
      phase: "succeeded",
      currentStepId: null,
      terminalOutcome: "approved",
      steps: completed,
      humanDecision: {
        stepId: "human-review",
        approved: true,
        outcome: "approved",
        summary: "Reads right.",
      },
    });
    let settled = false;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1/human-review" && init?.method === "POST") {
        settled = true;
        return json(decided);
      }
      if (path === "/api/runs/run-1") return json(settled ? decided : awaiting);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();

    const { container } = render(<RunDetailPage runId="run-1" />);

    const note = await screen.findByRole("textbox", { name: "Decision note" });
    expect(note).toHaveAttribute(
      "placeholder",
      "Optional — why this WorkOrder was approved or rejected.",
    );
    await user.type(note, "Reads right.");
    await user.click(screen.getByRole("button", { name: "Approve" }));

    // The whole run moved on the one response, so the page is never shown a
    // half-decided state: the phase, the outcome, and the prompt agree.
    expect(await screen.findByText("succeeded")).toBeVisible();
    expect(
      within(container.querySelector(".stats") as HTMLElement).getByText("approved"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1/human-review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: true, summary: "Reads right." }),
      }),
    );
  });

  it("records a rejection as the decision it was", async () => {
    const completed = run().steps.map((step) => ({ ...step, status: "completed" }));
    const awaiting = run({
      phase: "awaiting_human_review",
      currentStepId: "human-review",
      steps: completed,
      pendingHumanReview: {
        stepId: "human-review",
        title: "Review implementation for task-1",
        summary: "Implementation: done",
        prUrl: null,
      },
    });
    const decided = run({
      phase: "failed",
      currentStepId: null,
      terminalOutcome: "rejected",
      steps: completed,
      humanDecision: {
        stepId: "human-review",
        approved: false,
        outcome: "rejected",
        summary: "The greeting is wrong.",
      },
    });
    // Held open, so the buttons can be read mid-decision: the pressed one is
    // the only thing on the page that says which decision is in flight, and a
    // run must not be able to take two.
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let settled = false;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1/human-review" && init?.method === "POST") {
        await held;
        settled = true;
        return json(decided);
      }
      if (path === "/api/runs/run-1") return json(settled ? decided : awaiting);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();

    const { container } = render(<RunDetailPage runId="run-1" />);

    const note = await screen.findByRole("textbox", { name: "Decision note" });
    await user.type(note, "The greeting is wrong.");
    await user.click(screen.getByRole("button", { name: "Reject" }));

    expect(await screen.findByRole("button", { name: "Rejecting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();

    await act(async () => {
      release();
      await held;
    });

    // The rejection is terminal in the other direction, and the page has to
    // say so: a decision recorded as approved here is the failure this asserts
    // against, and it is not one the run offers a way back from.
    expect(await screen.findByText("failed")).toBeVisible();
    expect(
      within(container.querySelector(".stats") as HTMLElement).getByText("rejected"),
    ).toBeVisible();
    expect(container.querySelector(".callout-rejected")).toHaveTextContent(
      "The greeting is wrong.",
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1/human-review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: false, summary: "The greeting is wrong." }),
      }),
    );
  });

  it("keeps the decision available after one is refused", async () => {
    const awaiting = run({
      phase: "awaiting_human_review",
      currentStepId: "human-review",
      pendingHumanReview: {
        stepId: "human-review",
        title: "Review implementation for task-1",
        summary: "Implementation: done",
        prUrl: null,
      },
    });
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
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "run is not awaiting human review",
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("links to the pull request awaiting review", async () => {
    const awaiting = run({
      phase: "awaiting_human_review",
      currentStepId: "human-review",
      pendingHumanReview: {
        stepId: "human-review",
        title: "Review implementation for task-1",
        summary: "Implementation: done",
        prUrl: "https://github.com/acme/api/pull/42",
      },
    });
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(awaiting);
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);

    render(<RunDetailPage runId="run-1" />);

    const link = await screen.findByRole("link", { name: /view pull request/i });
    expect(link).toHaveAttribute("href", "https://github.com/acme/api/pull/42");
  });

  it("returns a finished run to the step a new message reopened", async () => {
    vi.useFakeTimers();
    const finished = run({
      phase: "succeeded",
      currentStepId: null,
      terminalOutcome: "approved",
      steps: run().steps.map((step) => ({ ...step, status: "completed" })),
    });
    let reopened = false;
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(reopened ? run() : finished);
      if (path === "/api/threads/instance")
        return json({
          id: "instance",
          title: "Implementation",
          archived: false,
          agentId: "agent",
          runner: "codex",
          workspaceRoot: "/worktrees/ws-1",
          workspaceRef: "engine/ws-1",
          workspaceAttached: true,
        });
      return json({ error: "not found" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetch);

    const { container } = render(<RunDetailPage runId="run-1" />);
    await act(async () => {});
    expect(container.querySelector(".stats")).toHaveTextContent("approved");
    expect(container.querySelector(".step[data-live]")).toBeNull();

    // Writing to the implementation's conversation puts the run back to work,
    // and the page has to follow a run it had already seen finish.
    reopened = true;
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(within(container.querySelector(".detail-title") as HTMLElement).getByText(
      "Implementation",
    )).toBeVisible();
    expect(container.querySelector(".stages .stage[data-status='in_progress']")).toHaveTextContent(
      "Implementation",
    );
    expect(container.querySelector(".step[data-live]")).toHaveTextContent("Implementation");
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
