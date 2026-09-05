import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  api,
  completeHumanReview,
  decideGraphApproval,
  deleteRun,
  getGraphEvents,
  getGraphRun,
  milestoneDetailsUrl,
  type ApiMilestone,
  type ApiGraphEvent,
  type ApiGraphRun,
  type ApiGraphTopology,
  type ApiProject,
  type ApiRunStep,
  type ApiWorkflowRun,
  type ApiWorkflowRunListing,
  type ApprovalDecision,
  type EngineConfig,
} from "./api";
import { Stat, StatStrip } from "./brand";
import { useProjectMilestones } from "./milestone-timeline";
import { WorkspaceControl } from "./workspace";

export const IN_PROGRESS_PHASES = new Set([
  "pending",
  "preparing_workspace",
  "running_agent",
]);

/** Where the unsent task prompt waits between visits to the new-workflow form.
 *  A prompt worth writing is worth several sittings, and navigating away — to
 *  check a run, or by closing the tab — should not be what throws it out. */
const WORKFLOW_DRAFT_KEY = "engine.workflowDraft";

export function phaseLabel(value: string) {
  return value.replaceAll("_", " ");
}

/** Whether a run has stopped moving.
 *
 *  The engine's own terminal test (`RunState.is_terminal`): every other phase
 *  is a run with somewhere left to go, including one parked on a human review.
 *  Not to be confused with `IN_PROGRESS_PHASES`, which is narrower on purpose
 *  -- it answers whether the engine is working on the run right now. */
export function runFinished(run: ApiWorkflowRunListing) {
  return run.phase === "succeeded" || run.phase === "failed";
}

/** Prefer the workflow's vocabulary while a run is active. The engine phase
 *  still drives behavior, but an operator cares which step is doing the work. */
export function runStatusLabel(run: ApiWorkflowRunListing) {
  if (!runFinished(run)) {
    const current = run.steps.find((step) => step.stepId === run.currentStepId);
    if (current) return current.name;
  }
  return phaseLabel(run.phase);
}

/** How loudly a run's phase should read. Failure is the only thing that gets
 *  the accent; a run still moving is ink, and one not started yet is a rule. */
export function phaseAccent(phase: string): "flame" | "quiet" | undefined {
  if (phase === "failed") return "flame";
  if (phase === "pending" || phase === "preparing_workspace") return "quiet";
  return undefined;
}

export function conversationCount(run: ApiWorkflowRunListing) {
  return run.steps.filter((step) => step.conversationUrl).length;
}

/** The runs behind both the workflow pages and the rail's Workflows section,
 *  kept current by the shell so every screen shows the same list. */
export function useRuns() {
  const [runs, setRuns] = useState<ApiWorkflowRunListing[]>([]);
  const [error, setError] = useState("");
  // An empty list means "nothing has been run" only once a poll has answered.
  // Until then it is what the state started as, and a page that reads this list
  // rather than owning it cannot tell the two apart without being told.
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<{ runs: ApiWorkflowRunListing[] }>("/api/runs")
        .then((value) => {
          if (cancelled) return;
          setRuns(value.runs);
          setError("");
          setLoaded(true);
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        })
        .finally(() => {
          if (!cancelled) timer = window.setTimeout(load, 1000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);
  // Deleting is not archiving: there is no list the run moves into and nothing
  // to restore it from, so the click is asked about before it is made. The row
  // then leaves on the click rather than a second later, when the poll next
  // reads the list -- and that same poll puts it back if the delete failed.
  const remove = (run: ApiWorkflowRunListing) => {
    if (!window.confirm(`Delete ${run.name}? This cannot be undone.`)) return;
    setRuns((current) => current.filter((item) => item.runId !== run.runId));
    void deleteRun(run.runId).catch(() => {});
  };
  return { runs, error, loaded, remove };
}

export function RunsPage({ runs, error }: { runs: ApiWorkflowRunListing[]; error: string }) {
  const [filter, setFilter] = useState<string>("");

  // Built from the phases actually present rather than from a fixed list, so a
  // filter is never offered that would empty the page, and a phase the workflow
  // grows later still gets a button.
  const phases = useMemo(() => {
    const seen: string[] = [];
    for (const run of runs) if (!seen.includes(run.phase)) seen.push(run.phase);
    return seen;
  }, [runs]);
  const shown = filter ? runs.filter((run) => run.phase === filter) : runs;

  const awaiting = runs.filter((run) => run.phase === "awaiting_human_review").length;
  const failed = runs.filter((run) => run.phase === "failed").length;
  const conversations = runs.reduce((total, run) => total + conversationCount(run), 0);

  return (
    <main className="panel-scroll">
      <header className="hero">
        <p className="eyebrow">OpenEngine / Work</p>
        <h1>WorkOrders</h1>
        <p className="lede">
          Each WorkOrder brings the task, agent steps, outputs, and human decision together.
        </p>
      </header>
      <StatStrip>
        <Stat label="WorkOrders" value={runs.length} />
        <Stat label="Awaiting review" value={awaiting} tone={awaiting ? "alert" : undefined} />
        <Stat label="Failed" value={failed} tone={failed ? "alert" : undefined} />
        <Stat label="Conversations" value={conversations} />
      </StatStrip>
      {runs.length > 0 && (
        <div className="toolbar">
          <div className="segmented" role="group" aria-label="Filter WorkOrders by phase">
            <button type="button" aria-pressed={!filter} onClick={() => setFilter("")}>
              All
            </button>
            {phases.map((phase) => (
              <button
                key={phase}
                type="button"
                aria-pressed={filter === phase}
                onClick={() => setFilter(filter === phase ? "" : phase)}
              >
                {phaseLabel(phase)}
              </button>
            ))}
          </div>
          <div className="toolbar-end">
            <span className="micro">
              {shown.length} of {runs.length} shown
            </span>
            <a className="btn" href="/runs/new">
              New WorkOrder
            </a>
          </div>
        </div>
      )}
      {error ? (
        <p className="notice notice-block">
          Could not load WorkOrders: {error}
        </p>
      ) : runs.length ? (
        <div className="cards">
          {shown.map((run) => {
            const current = run.steps.find((step) => step.stepId === run.currentStepId);
            return (
              <a
                className="card"
                data-accent={phaseAccent(run.phase)}
                href={`/runs/${run.runId}`}
                key={run.runId}
              >
                <div className="card-top">
                  <span className={`chip ${run.phase === "pending" ? "chip-flame" : ""}`}>
                    {runStatusLabel(run)}
                  </span>
                  <code className="card-id">{run.runId}</code>
                </div>
                <h2>{run.name}</h2>
                <dl className="card-stats">
                  <div>
                    <dt>Steps</dt>
                    <dd>{run.steps.length}</dd>
                  </div>
                  <div>
                    <dt>Chats</dt>
                    <dd>{conversationCount(run)}</dd>
                  </div>
                  <div>
                    <dt>Stage</dt>
                    <dd>{current?.name ?? run.terminalOutcome ?? "—"}</dd>
                  </div>
                </dl>
                <footer>
                  {run.workflowVersion
                    ? `${run.workflowName} · ${run.workflowVersion}`
                    : run.workflowName}
                </footer>
              </a>
            );
          })}
        </div>
      ) : (
        <div className="empty">
          <h2>No WorkOrders yet.</h2>
        </div>
      )}
    </main>
  );
}

export function NewWorkflowPage({
  config,
  project,
  milestone,
}: {
  config: EngineConfig;
  project?: ApiProject;
  milestone?: ApiMilestone;
}) {
  const [prompt, setPrompt] = useState(
    () => window.localStorage.getItem(WORKFLOW_DRAFT_KEY) ?? "",
  );
  const [repository, setRepository] = useState(".");
  const [runner, setRunner] = useState(config.defaultWorkflowRunner);
  const [workflowId, setWorkflowId] = useState(config.workflows[0]?.id ?? "");
  const [workstreamId, setWorkstreamId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  // A graph workflow — the [BETA] kind — names the agent it runs, so there is
  // one entry per agent and nothing left for a runner field to decide. Asking
  // anyway would be a control that looks like a choice and is not: the server
  // reads no runner for these.
  const selected = config.workflows.find((workflow) => workflow.id === workflowId);
  const picksItsOwnAgent = selected?.kind === "graph";

  useEffect(() => {
    if (prompt) window.localStorage.setItem(WORKFLOW_DRAFT_KEY, prompt);
    else window.localStorage.removeItem(WORKFLOW_DRAFT_KEY);
  }, [prompt]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const run = await api<ApiWorkflowRun>("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          workflowId,
          prompt,
          repository,
          ...(picksItsOwnAgent ? {} : { runner }),
          ...(milestone
            ? { milestoneId: milestone.milestoneId, workstreamId: workstreamId || undefined }
            : {}),
        }),
      });
      // The run now owns this prompt, so the draft has nothing left to keep.
      window.localStorage.removeItem(WORKFLOW_DRAFT_KEY);
      window.location.assign(`/runs/${encodeURIComponent(run.runId)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create WorkOrder");
      setSubmitting(false);
    }
  }

  return (
    <main className="panel-scroll">
      <header className="hero hero-narrow">
        <p className="eyebrow">
          {milestone ? `${project?.name ?? "Project"} / ${milestone.name}` : "OpenEngine / New WorkOrder"}
        </p>
        <h1>{milestone ? "Create a task" : "Create a WorkOrder"}</h1>
        <p className="lede">
          {milestone
            ? "Start work for this milestone, optionally under one of its workstreams."
            : "Create one WorkOrder that keeps its stages, agent conversations, outputs, and final human decision together."}
        </p>
      </header>
      <form className="form" onSubmit={submit}>
        <label>
          <span>Workflow definition</span>
          <select
            required
            value={workflowId}
            onChange={(event) => setWorkflowId(event.target.value)}
          >
            {/* The version is only shown when there is one. A [BETA] graph
                workflow has no version yet, and "name · " reads like something
                failed to load. */}
            {config.workflows.map((workflow) => (
              <option key={workflow.id} value={workflow.id}>
                {workflow.version ? `${workflow.name} · ${workflow.version}` : workflow.name}
              </option>
            ))}
          </select>
        </label>
        {milestone && (
          <label>
            <span>Workstream (optional)</span>
            <select
              value={workstreamId}
              onChange={(event) => setWorkstreamId(event.target.value)}
            >
              <option value="">No workstream — milestone task</option>
              {milestone.workstreams.map((workstream) => (
                <option key={workstream.workstreamId} value={workstream.workstreamId}>
                  {workstream.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          <span>Repository</span>
          <input
            required
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="owner/repository or local path"
          />
        </label>
        {picksItsOwnAgent ? (
          <p className="form-note">
            This workflow runs the agent named in its own definition, so there is no
            runner to choose.
          </p>
        ) : (
          <label>
            <span>Implementation runner</span>
            <select
              required
              value={runner}
              onChange={(event) => setRunner(event.target.value)}
            >
              {config.workflowRunners.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          <span>Task prompt</span>
          <textarea
            required
            rows={9}
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="Describe what the implementation agent should change and what success looks like."
          />
        </label>
        {error && (
          <p className="notice" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <a
            className="back-link"
            href={
              milestone && project
                ? milestoneDetailsUrl(project.projectId, milestone.milestoneId)
                : "/runs"
            }
          >
            Cancel
          </a>
          <button
            className="btn btn-primary"
            disabled={submitting || (!runner && !picksItsOwnAgent) || !workflowId}
            type="submit"
          >
            {submitting ? "Creating…" : milestone ? "Create task" : "Create WorkOrder"}
          </button>
        </div>
        <p className="form-note">
          The implementation starts after the WorkOrder is created. Reviewer execution is not
          available yet.
        </p>
      </form>
    </main>
  );
}

export function NewTaskPage({
  config,
  projectId,
  milestoneId,
}: {
  config: EngineConfig;
  projectId: string;
  milestoneId: string;
}) {
  const { project, milestones, loaded, error } = useProjectMilestones(projectId);
  const milestone = milestones.find((item) => item.milestoneId === milestoneId);

  if (!loaded)
    return (
      <main className="panel-scroll">
        <p className={error ? "notice notice-block" : "state-inline"}>
          {error ? `Could not load milestone: ${error}` : "Loading milestone…"}
        </p>
      </main>
    );
  if (!project || !milestone)
    return (
      <main className="panel-scroll">
        <p className="notice notice-block">
          This project&rsquo;s plan has no milestone {milestoneId}.
        </p>
      </main>
    );
  return <NewWorkflowPage config={config} project={project} milestone={milestone} />;
}

function StageProgress({ run }: { run: ApiWorkflowRun }) {
  const preparing = run.phase === "pending" || run.phase === "preparing_workspace";
  const stages = [
    {
      id: "workspace",
      name: run.phase === "pending" ? "Queued" : "Workspace",
      status: preparing ? "in_progress" : "completed",
    },
    ...run.steps.map((step) => ({ id: step.stepId, name: step.name, status: step.status })),
  ];
  return (
    <ol className="stages" aria-label="Current WorkOrder stage">
      {stages.map((stage) => (
        <li
          className="stage"
          data-status={stage.status}
          aria-current={
            stage.status === "in_progress" || stage.status === "action_required"
              ? "step"
              : undefined
          }
          key={stage.id}
        >
          <span>{stage.name}</span>
        </li>
      ))}
    </ol>
  );
}

/** The decision that ends a run, on the run it ends.
 *
 *  A run stops at human review and waits there indefinitely, and this is the
 *  only thing that moves it -- so it sits inside the callout that presents what
 *  the decision is made from rather than on a page of its own. The note is
 *  optional, because the button already says what was decided; it is where the
 *  reason goes, and the run keeps it as the decision's summary. */
function HumanReviewDecision({
  run,
  onDecided,
}: {
  run: ApiWorkflowRun;
  onDecided: (decided: ApiWorkflowRun) => void;
}) {
  const [note, setNote] = useState("");
  // Which button was pressed, so the one that is working says so and neither
  // can be pressed twice into two decisions on one run.
  const [deciding, setDeciding] = useState<"approve" | "reject">();
  const [error, setError] = useState("");

  async function decide(approved: boolean) {
    setDeciding(approved ? "approve" : "reject");
    setError("");
    try {
      onDecided(await completeHumanReview(run.runId, approved, note.trim()));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setDeciding(undefined);
    }
  }

  return (
    <div className="decision">
      <label>
        <span>Decision note</span>
        <textarea
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Optional — why this WorkOrder was approved or rejected."
        />
      </label>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      <div className="decision-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={deciding !== undefined}
          onClick={() => void decide(true)}
        >
          {deciding === "approve" ? "Approving…" : "Approve"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={deciding !== undefined}
          onClick={() => void decide(false)}
        >
          {deciding === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>
    </div>
  );
}

function StepCard({ step, current }: { step: ApiRunStep; current: boolean }) {
  return (
    <article
      className={`step ${current ? "step-current" : ""}`}
      // Work happening now, marked on the box rather than only in the chip that
      // names it -- the same thing the rail's live pip says about the run.
      data-live={step.status === "in_progress" || undefined}
    >
      <div className="step-rail" aria-hidden="true" />
      <div className="step-body">
        <header>
          <div>
            <span className="eyebrow">{step.kind} step</span>
            <h2>{step.name}</h2>
          </div>
          <span className={`chip ${step.status === "action_required" ? "chip-flame" : ""}`}>
            {phaseLabel(step.status)}
          </span>
        </header>
        {step.outcome && (
          <p className="step-outcome" data-changes={step.changesRequested || undefined}>
            Outcome: <strong>{phaseLabel(step.outcome)}</strong>
          </p>
        )}
        {step.summary && <p className="step-summary">{step.summary}</p>}
        {step.outputs.length > 0 && (
          <dl className="step-outputs">
            {step.outputs.map((output) => (
              <div key={output.name}>
                <dt>{output.name}</dt>
                <dd>{output.value}</dd>
              </div>
            ))}
          </dl>
        )}
        {step.agentId && (
          <div className="step-agent">
            <span>Agent {step.agentId}</span>
            {step.conversationUrl ? (
              <a className="link-flame" href={step.conversationUrl}>
                Open conversation →
              </a>
            ) : (
              <span>Conversation not started</span>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

function GraphApprovalDecision({
  runId,
  approval,
  onDecided,
}: {
  runId: string;
  approval: ApiGraphRun["pendingApprovals"][number];
  onDecided: (run: ApiGraphRun) => void;
}) {
  const [submitting, setSubmitting] = useState<string>();
  const [error, setError] = useState("");

  const decide = async (decision: ApprovalDecision) => {
    setSubmitting(decision);
    setError("");
    try {
      onDecided(await decideGraphApproval(runId, approval.approvalId, decision));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setSubmitting(undefined);
    }
  };

  return (
    <div className="decision">
      <label>
        <span>Decision note</span>
        <textarea rows={3} />
      </label>
      {error && <p className="notice">Could not record decision: {error}</p>}
      <div className="decision-actions">
        {approval.allowedDecisions.includes("accept") && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={submitting !== undefined}
            onClick={() => void decide("accept")}
          >
            {submitting === "accept" ? "Approving…" : "Approve"}
          </button>
        )}
        {approval.allowedDecisions.includes("accept_for_session") && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={submitting !== undefined}
            onClick={() => void decide("accept_for_session")}
          >
            {submitting === "accept_for_session" ? "Approving…" : "Approve for session"}
          </button>
        )}
        {approval.allowedDecisions.includes("cancel") && (
          <button
            type="button"
            className="btn"
            disabled={submitting !== undefined}
            onClick={() => void decide("cancel")}
          >
            {submitting === "cancel" ? "Rejecting…" : "Reject"}
          </button>
        )}
      </div>
    </div>
  );
}

export function RunDetailPage({ runId }: { runId: string }) {
  const [baseRun, setRun] = useState<ApiWorkflowRun>();
  const [graph, setGraph] = useState<ApiGraphRun>();
  const [topology, setTopology] = useState<ApiGraphTopology>();
  const [graphEvents, setGraphEvents] = useState<ApiGraphEvent[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    // Kept up even once the run has finished, the way the rail's list is: an
    // editable step reopens when its conversation is written to, so a page that
    // stopped reading at "succeeded" would go on saying so while the
    // implementation it names is working again.
    const load = async () => {
      try {
        const value = await api<ApiWorkflowRun>(`/api/runs/${encodeURIComponent(runId)}`);
        if (cancelled) return;
        setRun(value);
        if (!value.workflowVersion) {
          const [nextGraph, nextTopology, eventLog] = await Promise.all([
            getGraphRun(runId),
            api<ApiGraphTopology>(`/graph/api/graphs/${encodeURIComponent(value.workflowId)}`),
            getGraphEvents(runId),
          ]);
          if (cancelled) return;
          setGraph(nextGraph);
          setTopology(nextTopology);
          setGraphEvents(eventLog.events);
        }
        setError("");
      } catch (reason) {
        if (!cancelled) setError((reason as Error).message);
      } finally {
        if (!cancelled) timer = window.setTimeout(load, 1000);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);
  const shownRun = useMemo(() => {
    if (!baseRun || !graph || !topology) return baseRun;
    const completed = new Set(
      graphEvents.filter((event) => event.type === "node.finished").map((event) => event.nodeId),
    );
    const active = new Set(graph.activeExecutions.map((execution) => execution.nodeId));
    const waiting = new Set(graph.pendingApprovals.map((approval) => approval.nodeId));
    return {
      ...baseRun,
      phase: graph.status === "awaiting_approval" ? "awaiting_human_review" : baseRun.phase,
      currentStepId: graph.activeExecutions[0]?.nodeId ?? graph.nextNodes[0] ?? null,
      failureReason: graph.error || baseRun.failureReason,
      steps: topology.nodes.filter((node) => node.kind !== "workspace").map((node) => ({
        stepId: node.nodeId,
        name: node.name,
        kind: node.kind === "human" ? "human" as const : "agent" as const,
        status: waiting.has(node.nodeId) ? "action_required" : active.has(node.nodeId) ? "in_progress" : completed.has(node.nodeId) ? "completed" : "pending",
        outcome: completed.has(node.nodeId) ? "completed" : null,
        changesRequested: false,
        agentId: node.kind === "agent" ? baseRun.workflowId.split("-").at(-1) ?? null : null,
        agentInstanceId: null,
        agentRunId: null,
        conversationId: null,
        conversationUrl: graphEvents.some((event) => event.nodeId === node.nodeId && (
          event.type === "conversation.started" || event.type === "transcript"
        ))
          ? `/runs/${encodeURIComponent(runId)}/conversations/graph--${encodeURIComponent(node.nodeId)}` : null,
        waiting: waiting.has(node.nodeId),
        summary: typeof graph.values[node.nodeId] === "string" ? String(graph.values[node.nodeId]) : "",
        outputs: [],
      })),
      pendingHumanReview: graph.pendingApprovals[0] ? {
        stepId: graph.pendingApprovals[0].nodeId,
        title: graph.pendingApprovals[0].reason || "Review this WorkOrder",
        summary: "",
        prUrl: null,
      } : null,
    } satisfies ApiWorkflowRun;
  }, [baseRun, graph, topology, graphEvents, runId]);
  const run = shownRun;
  const workspaceThreadId = run
    ? (run.steps.find(
        (step) => step.stepId === run.currentStepId && step.agentInstanceId,
      )?.agentInstanceId ??
      run.steps.find((step) => step.agentInstanceId)?.agentInstanceId)
    : undefined;

  return (
    <main className="panel-scroll">
      {error ? (
        <p className="notice notice-block">
          Could not load WorkOrder: {error}
        </p>
      ) : !run ? (
        <p className="state-inline">Loading WorkOrder…</p>
      ) : (
        <>
          <header className="detail-head">
            <a href="/runs" className="back-link">
              ← All WorkOrders
            </a>
            <div className="detail-title">
              <div>
                <p className="eyebrow">
                  {run.workflowVersion
                    ? `${run.workflowName} / ${run.workflowVersion}`
                    : run.workflowName}
                </p>
                <h1>{run.name}</h1>
                <p className="lede">{run.taskPrompt}</p>
              </div>
              <span className={`chip ${phaseAccent(run.phase) === "flame" ? "chip-flame" : "chip-ink"}`}>
                {runStatusLabel(run)}
              </span>
            </div>
          </header>
          <StatStrip>
            <Stat label="Run ID" value={run.runId} />
            <Stat label="Repository" value={run.repository} />
            <Stat label="Current step" value={run.currentStepId ?? "—"} />
            <Stat label="Final outcome" value={run.terminalOutcome ?? "In progress"} />
          </StatStrip>
          {graph && typeof graph.values.workspace === "string" ? (
            <section className="run-workspace" aria-label="WorkOrder checkout">
              <div className="workspace-control"><span className="micro">Working in</span><code className="dock-path">cd {graph.values.workspace}</code></div>
            </section>
          ) : workspaceThreadId && (
            <section className="run-workspace" aria-label="WorkOrder checkout">
              <WorkspaceControl threadId={workspaceThreadId} />
            </section>
          )}
          <StageProgress run={run} />
          {/* A [BETA] WorkOrder's stages are the graph's nodes, so having none
              means the graph engine could not be read -- not that the run has
              no stages. Saying so beats a page that looks like a WorkOrder
              which never started. */}
          {run.steps.length === 0 && !run.workflowVersion && (
            <section className="callout">
              <p className="eyebrow">Beta workflow</p>
              <p>
                This WorkOrder runs on the graph engine, and its stages could not
                be read from it. They are served under{" "}
                <code>/graph/api/runs/{run.runId}</code>.
              </p>
            </section>
          )}
          {run.pendingHumanReview && graph?.pendingApprovals[0] ? (
            <section className="callout callout-action">
              <p className="eyebrow">Action required</p>
              <h2>{run.pendingHumanReview.title}</h2>
              <GraphApprovalDecision
                runId={runId}
                approval={graph.pendingApprovals[0]}
                onDecided={setGraph}
              />
            </section>
          ) : run.pendingHumanReview && (
            <section className="callout callout-action">
              <p className="eyebrow">Action required</p>
              <h2>{run.pendingHumanReview.title}</h2>
              <p>
                The implementation and agent review are complete. A human approval or rejection
                is the final decision.
              </p>
              {run.pendingHumanReview.prUrl && (
                <p>
                  <a href={run.pendingHumanReview.prUrl} target="_blank" rel="noreferrer">
                    View pull request ↗
                  </a>
                </p>
              )}
              <HumanReviewDecision run={run} onDecided={setRun} />
            </section>
          )}
          {run.humanDecision && (
            <section
              className={`callout ${run.humanDecision.outcome === "rejected" ? "callout-rejected" : ""}`}
            >
              <p className="eyebrow">Final human decision</p>
              <h2>{run.humanDecision.outcome}</h2>
              <p>{run.humanDecision.summary || "No decision summary was provided."}</p>
            </section>
          )}
          {run.failureReason && (
            <p className="notice notice-block">
              {run.failureReason}
            </p>
          )}
          <section className="timeline" aria-label="WorkOrder steps">
            {run.steps.map((step) => (
              <StepCard
                key={step.stepId}
                step={step}
                current={run.currentStepId === step.stepId}
              />
            ))}
          </section>
        </>
      )}
    </main>
  );
}
