import { useAui, useAuiState } from "@assistant-ui/react";
import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  api,
  newChatAgent,
  setProjectArchived,
  setThreadAutoApprove,
  setThreadRunner,
  type ApiThread,
  type ApiProject,
  type EngineConfig,
  type RunnerOption,
} from "./api";
import { ChatThread, ConversationStats } from "./chat";
import { GraphConversationPage } from "./graph-conversation";
import { MilestoneDetailsPage } from "./milestone-details";
import { MilestoneTimeline } from "./milestone-timeline";
import { ProjectMilestonesPage } from "./project-milestones";
import { EngineRuntimeProvider } from "./runtime";
import { NewTaskPage, NewWorkflowPage, RunDetailPage, RunsPage, useRuns } from "./runs";
import { routeForPath, type Route } from "./routes";
import { Sidebar, type RailSection } from "./sidebar";
import "./styles.css";

function ChatPanel({
  config,
  agentId,
  runner,
  planning,
  newProject,
  project,
  onAgentChange,
  onRunnerChange,
}: {
  config: EngineConfig;
  agentId: string;
  runner: string;
  planning: boolean;
  newProject: boolean;
  project?: ApiProject;
  onAgentChange: (agentId: string) => void;
  onRunnerChange: (runner: string) => void;
}) {
  return (
    <main className={`panel ${planning ? "panel-project" : ""}`}>
      <ChatHeader
        config={config}
        agentId={agentId}
        runner={runner}
        compact={planning}
        onAgentChange={onAgentChange}
        onRunnerChange={onRunnerChange}
      />
      {!planning && <ConversationStats />}
      <ChatThread project={planning} />
      {planning && (
        <MilestoneTimeline project={project} collapsedUntilMilestone={newProject} />
      )}
    </main>
  );
}

type ThreadCustom = {
  agentId?: string;
  runner?: string;
  workflowRunId?: string;
  editable?: boolean;
  autoApprove?: boolean;
};

/** The header speaks for whatever is on screen: the defaults the next
 *  conversation starts from, or the open conversation and who answers it. */
function ChatHeader({
  config,
  agentId,
  runner,
  compact,
  onAgentChange,
  onRunnerChange,
}: {
  config: EngineConfig;
  agentId: string;
  runner: string;
  compact: boolean;
  onAgentChange: (agentId: string) => void;
  onRunnerChange: (runner: string) => void;
}) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | ThreadCustom
    | undefined;

  if (remoteId)
    return (
      // Keyed by conversation: switching chats must not leave the previous
      // one's in-flight choice on screen.
      <ConversationHeader
        key={remoteId}
        threadId={remoteId}
        listed={custom}
        runners={config.runners}
        workflowRunners={config.workflowRunners}
        fallbackRunner={runner}
        compact={compact}
      />
    );

  return (
    <header className={`panel-head ${compact ? "panel-head-compact" : ""}`}>
      <div className="panel-head-copy">
        <p className="eyebrow">{compact ? "New project" : "New chat defaults"}</p>
        <h1>{compact ? "Define a new project with milestones" : "New conversation"}</h1>
        {!compact && (
          <p className="lede">Choose what starts the next conversation and which runner answers.</p>
        )}
      </div>
      <label className="field">
        <span>Agent</span>
        <select
          className="field-box"
          value={agentId}
          onChange={(event) => onAgentChange(event.target.value)}
        >
          {config.agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.id} — {agent.description}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Runner</span>
        <select
          className="field-box"
          value={runner}
          onChange={(event) => onRunnerChange(event.target.value)}
        >
          {config.runners.map((option) => (
            <option key={option.id} value={option.id}>
              {option.id}
            </option>
          ))}
        </select>
      </label>
    </header>
  );
}

/** The open conversation's own runner, which the server remembers between
 *  turns. Its agent was settled when the chat was created, so that one is
 *  shown as the fact it is. */
function ConversationHeader({
  threadId,
  listed,
  runners,
  workflowRunners,
  fallbackRunner,
  compact,
}: {
  threadId: string;
  listed?: ThreadCustom;
  runners: RunnerOption[];
  workflowRunners: string[];
  fallbackRunner: string;
  compact: boolean;
}) {
  const aui = useAui();
  const listedTitle = useAuiState((state) => state.threadListItem.title);
  // Read the conversation rather than trusting the cached thread list for
  // this: the dropdown claims to name the runner that answers here, and the
  // list is a snapshot taken whenever it was last refreshed.
  const [fetched, setFetched] = useState<ApiThread>();
  const [chosen, setChosen] = useState<string>();
  const [chosenAutoApprove, setChosenAutoApprove] = useState<boolean>();
  const [autoApproveBusy, setAutoApproveBusy] = useState(false);
  const [error, setError] = useState<string>();
  const thread = fetched ?? listed;
  // A chat nothing has described yet was started on the defaults, so those are
  // the truthful thing to show while it is being read.
  const runner = chosen ?? thread?.runner ?? fallbackRunner;
  const workflowConversation = Boolean(thread?.workflowRunId);
  const availableRunners = workflowConversation
    ? workflowRunners.map((id) => ({ id, implementation: id }))
    : runners;
  const autoApprove = chosenAutoApprove ?? thread?.autoApprove ?? false;
  // Title generation refreshes the thread list after the first message. That
  // refreshed value can be newer than the conversation snapshot fetched when
  // this header first mounted.
  const title = listedTitle || fetched?.title || "New chat";

  useEffect(() => {
    let current = true;
    void api<ApiThread>(`/api/threads/${threadId}`)
      .then((value) => {
        if (current) setFetched(value);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [threadId]);

  async function choose(next: string) {
    setChosen(next);
    setError(undefined);
    try {
      setFetched(await setThreadRunner(threadId, next));
      // The sidebar prints the same runner under every chat's title.
      await aui.threads.reload();
    } catch (failure) {
      setChosen(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  async function chooseAutoApprove(next: boolean) {
    setChosenAutoApprove(next);
    setAutoApproveBusy(true);
    setError(undefined);
    try {
      setFetched(await setThreadAutoApprove(threadId, next));
    } catch (failure) {
      setChosenAutoApprove(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setAutoApproveBusy(false);
    }
  }

  return (
    <header
      className={`panel-head ${workflowConversation ? "panel-head-workflow" : ""} ${compact ? "panel-head-compact" : ""}`}
    >
      <div className="panel-head-copy">
        <p className="eyebrow">{compact ? "This project" : "This conversation"}</p>
        <h1>{title}</h1>
        {workflowConversation && (
          <p className="lede">
            {thread?.editable
              ? "A WorkOrder step owns this transcript; sending guidance reactivates it if it has closed."
              : "A WorkOrder step owns this read-only transcript."}
          </p>
        )}
      </div>
      <div className="field">
        <span>Agent</span>
        <span className="field-box">{thread?.agentId ?? "…"}</span>
      </div>
      <label className="field">
        <span>Runner</span>
        <select
          className="field-box"
          value={runner}
          onChange={(event) => void choose(event.target.value)}
        >
          {availableRunners.map((option) => (
            <option key={option.id} value={option.id}>
              {option.id}
            </option>
          ))}
        </select>
        {error && <span className="field-error">{error}</span>}
      </label>
      {workflowConversation && (
        <label className="field">
          <span>Approvals</span>
          <span className="field-box auto-approve-control">
            <input
              type="checkbox"
              checked={autoApprove}
              disabled={autoApproveBusy}
              onChange={(event) => void chooseAutoApprove(event.target.checked)}
            />
            <span>{autoApproveBusy ? "Saving…" : "Auto-approve"}</span>
          </span>
          {error && <span className="field-error">{error}</span>}
        </label>
      )}
    </header>
  );
}

function currentRoute(): Route {
  return routeForPath(window.location.pathname);
}

/** Which section of the rail the page on screen came from, so the rail opens
 *  showing where you are. A workflow's own conversation belongs to its run;
 *  other conversations open alongside projects. */
function sectionFor(route: Route): RailSection {
  if (route.kind === "graph-conversation") return "workflows";
  if (route.kind === "chat")
    return route.runId ? "workflows" : "projects";
  return route.kind === "project" || route.kind === "milestone" ? "projects" : "workflows";
}

/** `/plan` is where a plan starts, not where it lives.
 *
 *  Once the conversation exists it has a URL of its own, and taking it means a
 *  refresh reopens the plan being written rather than starting a second empty
 *  one. Replaced rather than pushed: Back belongs to whatever page sent you
 *  here, and there is nothing at `/plan` to return to. */
function PlanPermalink() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  useEffect(() => {
    if (remoteId) window.history.replaceState(null, "", `/conversations/${remoteId}`);
  }, [remoteId]);
  return null;
}

function useProjects() {
  const [projects, setProjects] = useState<ApiProject[]>([]);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<{ projects: ApiProject[] }>("/api/projects")
        .then((value) => {
          if (!cancelled) setProjects(value.projects);
        })
        .catch(() => {})
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
  // The row moves on the click rather than a second later, when the poll next
  // reads the list -- and that same poll is what puts it back if the write
  // failed, so a click that did not take undoes itself.
  const archive = (project: ApiProject, archived: boolean) => {
    setProjects((current) =>
      current.map((item) =>
        item.projectId === project.projectId ? { ...item, archived } : item,
      ),
    );
    void setProjectArchived(project.projectId, archived).catch(() => {});
  };
  return { projects, archive };
}

/** One shell for every screen: the rail, and the page beside it.
 *
 *  The chat runtime is mounted around every page so conversation routes can
 *  render their thread while workflow pages keep their own run UI. */
function App() {
  const route = useMemo(currentRoute, []);
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");
  const { runs, error: runsError, loaded: runsLoaded, remove: deleteRun } = useRuns();
  const { projects, archive: archiveProject } = useProjects();

  // Settled for this mount: the route is read once, and every move between
  // pages here is a full page load.
  const plan = route.kind === "chat" && Boolean(route.plan);

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        // The plan page is the new chat page with its agent already chosen.
        setAgentId(newChatAgent(value, plan));
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [plan]);

  if (error)
    return <main className="state state-fatal">Could not connect to openengine: {error}</main>;
  if (!config || !agentId || !runner)
    return <main className="state">Starting openengine…</main>;

  const chat = route.kind === "chat";
  const activeRunId = route.kind === "run" || route.kind === "chat" || route.kind === "graph-conversation" ? route.runId : undefined;
  // The conversation on screen, and the only thing that says whether it is a
  // project's: a plan's URL is an ordinary chat's, so the projects list is what
  // tells them apart. It arrives after the first paint, and the rail follows.
  const conversationUrl = chat ? window.location.pathname.replace(/\/$/, "") : undefined;
  const activeProject = projects.find((project) => project.conversationUrl === conversationUrl);
  const projectPage = activeProject !== undefined;
  const sidebar = () => (
    <Sidebar
      projects={projects}
      runs={runs}
      initialSection={sectionFor(route)}
      activeRunId={activeRunId}
      activeConversationUrl={conversationUrl}
      activeProjectId={
        route.kind === "project" || route.kind === "milestone" || route.kind === "new-task"
          ? route.projectId
          : undefined
      }
      activeMilestonesPage={route.kind === "project"}
      activeView={route.kind === "runs" ? "runs" : route.kind === "new-run" ? "new" : undefined}
      onArchiveProject={archiveProject}
      onDeleteRun={deleteRun}
    />
  );
  return (
    <EngineRuntimeProvider
      defaults={{ agentId, runner, createProject: plan }}
      initialThreadId={chat ? route.threadId : undefined}
      rememberActiveThread={chat}
      // The plan page opens on a new conversation rather than the last one:
      // a New Project button that handed you back the chat you were in would not be
      // a plan. What it starts is still an ordinary chat to come back to.
      restoreActiveThread={chat && !plan}
      deferMount={chat}
      fallback={
        <div className="app-shell">
          {sidebar()}
          <main className="loading">Restoring chat…</main>
        </div>
      }
    >
      <div className="app-shell">
        {plan && <PlanPermalink />}
        {/* Every conversation page, not just a workflow's, marks its owning
            project or WorkOrder in the rail. */}
        {sidebar()}
        {route.kind === "runs" ? (
          <RunsPage runs={runs} error={runsError} />
        ) : route.kind === "new-run" ? (
          <NewWorkflowPage config={config} />
        ) : route.kind === "new-task" ? (
          <NewTaskPage
            config={config}
            projectId={route.projectId}
            milestoneId={route.milestoneId}
          />
        ) : route.kind === "run" ? (
          <RunDetailPage runId={route.runId} />
        ) : route.kind === "graph-conversation" ? (
          <GraphConversationPage runId={route.runId} nodeId={route.nodeId} />
        ) : route.kind === "project" ? (
          <ProjectMilestonesPage projectId={route.projectId} />
        ) : route.kind === "milestone" ? (
          <MilestoneDetailsPage
            projectId={route.projectId}
            milestoneId={route.milestoneId}
            // The list the shell already follows for the rail and the runs
            // page: a task is a run started in a workstream, so this page reads
            // the same poll rather than opening one of its own.
            runs={runs}
            runsError={runsError}
            runsLoaded={runsLoaded}
          />
        ) : (
          <ChatPanel
            config={config}
            agentId={agentId}
            runner={runner}
            planning={plan || projectPage}
            newProject={plan}
            project={activeProject}
            onAgentChange={setAgentId}
            onRunnerChange={setRunner}
          />
        )}
      </div>
    </EngineRuntimeProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
