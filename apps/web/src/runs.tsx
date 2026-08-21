import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  api,
  completeHumanReview,
  type ApiRunStep,
  type ApiWorkflowRun,
  type EngineConfig,
} from "./api";
import { Stat, StatStrip } from "./brand";
import { WorkspaceControl } from "./workspace";

export const IN_PROGRESS_PHASES = new Set([
  "pending",
  "preparing_workspace",
  "implementing",
  "reviewing",
]);

/** Where the unsent task prompt waits between visits to the new-workflow form.
 *  A prompt worth writing is worth several sittings, and navigating away — to
 *  check a run, or by closing the tab — should not be what throws it out. */
const WORKFLOW_DRAFT_KEY = "engine.workflowDraft";

export function phaseLabel(value: string) {
  return value.replaceAll("_", " ");
}

/** How loudly a run's phase should read. Failure is the only thing that gets
 *  the accent; a run still moving is ink, and one not started yet is a rule. */
export function phaseAccent(phase: string): "flame" | "quiet" | undefined {
  if (phase === "failed") return "flame";
  if (phase === "pending" || phase === "preparing_workspace") return "quiet";
  return undefined;
}

export function conversationCount(run: ApiWorkflowRun) {
  return run.steps.filter((step) => step.conversationUrl).length;
}

/** The runs behind both the workflow pages and the rail's Workflows section,
 *  kept current by the shell so every screen shows the same list. */
export function useRuns() {
  const [runs, setRuns] = useState<ApiWorkflowRun[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<{ runs: ApiWorkflowRun[] }>("/api/runs")
        .then((value) => {
          if (cancelled) return;
          setRuns(value.runs);
          setError("");
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
  return { runs, error };
}

export function RunsPage({ runs, error }: { runs: ApiWorkflowRun[]; error: string }) {
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
        <h1>Workflow runs</h1>
        <p className="lede">
          Each run brings the task, agent steps, outputs, and human decision together.
        </p>
      </header>
      <StatStrip>
        <Stat label="Runs" value={runs.length} />
        <Stat label="Awaiting review" value={awaiting} tone={awaiting ? "alert" : undefined} />
        <Stat label="Failed" value={failed} tone={failed ? "alert" : undefined} />
        <Stat label="Conversations" value={conversations} />
      </StatStrip>
      {runs.length > 0 && (
        <div className="toolbar">
          <div className="segmented" role="group" aria-label="Filter runs by phase">
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
              Run a workflow
            </a>
          </div>
        </div>
      )}
      {error ? (
        <p className="notice notice-block">
          Could not load runs: {error}
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
                    {phaseLabel(run.phase)}
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
                  {run.workflowName} · {run.workflowVersion}
                </footer>
              </a>
            );
          })}
        </div>
      ) : (
        <div className="empty">
          <h2>No workflow runs yet.</h2>
          <p>Chats remain available from the sidebar.</p>
        </div>
      )}
    </main>
  );
}

export function NewWorkflowPage({ config }: { config: EngineConfig }) {
  const [prompt, setPrompt] = useState(
    () => window.localStorage.getItem(WORKFLOW_DRAFT_KEY) ?? "",
  );
  const [repository, setRepository] = useState(".");
  const [runner, setRunner] = useState(config.defaultWorkflowRunner);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

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
          workflowId: "implementation-review-v1",
          prompt,
          repository,
          runner,
        }),
      });
      // The run now owns this prompt, so the draft has nothing left to keep.
      window.localStorage.removeItem(WORKFLOW_DRAFT_KEY);
      window.location.assign(`/runs/${encodeURIComponent(run.runId)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create workflow run");
      setSubmitting(false);
    }
  }

  return (
    <main className="panel-scroll">
      <header className="hero hero-narrow">
        <p className="eyebrow">OpenEngine / New workflow</p>
        <h1>Start a workflow</h1>
        <p className="lede">
          Create one run that keeps its stages, agent conversations, outputs, and final human
          decision together.
        </p>
      </header>
      <form className="form" onSubmit={submit}>
        <label>
          <span>Workflow definition</span>
          <select disabled value="implementation-review-v1">
            <option value="implementation-review-v1">Implementation review · v1</option>
          </select>
        </label>
        <label>
          <span>Repository</span>
          <input
            required
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="owner/repository or local path"
          />
        </label>
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
          <a className="back-link" href="/runs">
            Cancel
          </a>
          <button className="btn btn-primary" disabled={submitting || !runner} type="submit">
            {submitting ? "Creating…" : "Create workflow run"}
          </button>
        </div>
        <p className="form-note">
          The implementation starts after the run is created. Reviewer execution is not
          available yet.
        </p>
      </form>
    </main>
  );
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
    <ol className="stages" aria-label="Current workflow stage">
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
          placeholder="Optional — why this run was approved or rejected."
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
    <article className={`step ${current ? "step-current" : ""}`}>
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

export function RunDetailPage({ runId }: { runId: string }) {
  const [run, setRun] = useState<ApiWorkflowRun>();
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<ApiWorkflowRun>(`/api/runs/${encodeURIComponent(runId)}`)
        .then((value) => {
          if (cancelled) return;
          setRun(value);
          setError("");
          if (IN_PROGRESS_PHASES.has(value.phase)) {
            timer = window.setTimeout(load, 1000);
          }
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);
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
          Could not load run: {error}
        </p>
      ) : !run ? (
        <p className="state-inline">Loading workflow run…</p>
      ) : (
        <>
          <header className="detail-head">
            <a href="/runs" className="back-link">
              ← All workflow runs
            </a>
            <div className="detail-title">
              <div>
                <p className="eyebrow">
                  {run.workflowName} / {run.workflowVersion}
                </p>
                <h1>{run.name}</h1>
                <p className="lede">{run.taskPrompt}</p>
              </div>
              <span className={`chip ${phaseAccent(run.phase) === "flame" ? "chip-flame" : "chip-ink"}`}>
                {phaseLabel(run.phase)}
              </span>
            </div>
          </header>
          <StatStrip>
            <Stat label="Run ID" value={run.runId} />
            <Stat label="Repository" value={run.repository} />
            <Stat label="Current step" value={run.currentStepId ?? "—"} />
            <Stat label="Final outcome" value={run.terminalOutcome ?? "In progress"} />
          </StatStrip>
          {workspaceThreadId && (
            <section className="run-workspace" aria-label="Workflow checkout">
              <WorkspaceControl threadId={workspaceThreadId} />
            </section>
          )}
          <StageProgress run={run} />
          {run.pendingHumanReview && (
            <section className="callout callout-action">
              <p className="eyebrow">Action required</p>
              <h2>{run.pendingHumanReview.title}</h2>
              <p>
                The implementation and agent review are complete. A human approval or rejection
                is the final decision.
              </p>
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
          <section className="timeline" aria-label="Workflow steps">
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
