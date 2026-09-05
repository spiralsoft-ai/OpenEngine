import type { ThreadMessage } from "@assistant-ui/react";

export const RUN_NOT_STARTED_ERROR_CODE = "run-not-started";

/** Mark a submission failure that happened before the server accepted the turn. */
export function runNotStartedError(error: unknown) {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "The agent run could not be started.";
  return { code: RUN_NOT_STARTED_ERROR_CODE, message };
}

export type AgentOption = {
  id: string;
  description: string;
  instructions: string;
};

export type RunnerOption = {
  id: string;
  implementation: string;
};

export type EngineConfig = {
  agents: AgentOption[];
  runners: RunnerOption[];
  defaultAgent: string;
  /** The agent the New Project button talks to, empty when none is composed. */
  planAgent: string;
  defaultRunner: string;
  workflowRunners: string[];
  defaultWorkflowRunner: string;
  /** What a WorkOrder can be created from.
   *
   *  `kind` is which engine runs it. A `"graph"` is the newer, `[BETA]` kind:
   *  it has no version yet, and it names its own agent, so the form neither
   *  prints a version for one nor asks which runner to use. */
  workflows: { id: string; name: string; version: string; kind: "steps" | "graph" }[];
};

/** Which agent the next conversation starts on, for a plan or an ordinary chat.
 *
 *  A deployment that composed no planner says so with an empty `planAgent`, and
 *  the plan page then opens on the default rather than on nothing: the button
 *  starting a chat you can retarget is better than one that starts none. */
export function newChatAgent(config: EngineConfig, plan: boolean): string {
  return (plan && config.planAgent) || config.defaultAgent;
}

export type ApiThread = {
  id: string;
  title: string;
  archived: boolean;
  agentId: string;
  runner: string;
  /** The checkout, when this chat currently has one. */
  workspaceRoot?: string;
  /** What to check out to read this chat's work, attached or not. */
  workspaceRef?: string;
  workspaceAttached: boolean;
  workflowRunId?: string;
  workflowStepId?: string;
  editable?: boolean;
  autoApprove?: boolean;
};

export type ApiProject = {
  projectId: string;
  name: string;
  /** Put away rather than deleted: listed under Archived, and restorable. */
  archived: boolean;
  /** The planning conversation this project was named after, when it still has
   *  one. A project with none is listed but has nothing to open. */
  conversationUrl?: string;
  /** How many milestones the plan holds, from the responses that counted them.
   *  Absent where nothing counted, which is not the same as a plan of none. */
  milestoneCount?: number;
};

/** The page listing one project's plan in full. */
export function projectMilestonesUrl(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/milestones`;
}

/** One milestone's own page: the workstreams under it and the tasks in each.
 *
 *  Nested under the plan it belongs to rather than named by its id alone: the
 *  page is read as part of a project, and the way back out is the plan. */
export function milestoneDetailsUrl(
  projectId: string,
  milestoneId: string,
): string {
  return `${projectMilestonesUrl(projectId)}/${encodeURIComponent(milestoneId)}`;
}

export function milestoneNewTaskUrl(
  projectId: string,
  milestoneId: string,
): string {
  return `${milestoneDetailsUrl(projectId, milestoneId)}/tasks/new`;
}

export type ApiWorkstream = {
  workstreamId: string;
  name: string;
  /** The part of the milestone this workstream covers. */
  scope: string;
};

export type ApiMilestone = {
  milestoneId: string;
  name: string;
  description: string;
  dependencies: string[];
  workstreams: ApiWorkstream[];
};

export type ApiProjectMilestones = {
  project: ApiProject;
  milestones: ApiMilestone[];
};

export function getProjectMilestones(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiProjectMilestones> {
  return api<ApiProjectMilestones>(
    `/api/projects/${encodeURIComponent(projectId)}/milestones`,
    {
      signal,
    },
  );
}

export function createProject(
  name: string,
  signal?: AbortSignal,
): Promise<ApiProject> {
  return api<ApiProject>("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
    signal,
  });
}

/** Put a project away, or take it back out. */
export function setProjectArchived(
  projectId: string,
  archived: boolean,
): Promise<ApiProject> {
  return api<ApiProject>(
    `/api/projects/${encodeURIComponent(projectId)}/${archived ? "archive" : "unarchive"}`,
    { method: "POST" },
  );
}

/** A step as the runs list carries it: what it is and where it stands, which
 *  is all the rail and the cards read of one. */
export type ApiRunStepListing = {
  stepId: string;
  name: string;
  kind: "agent" | "human";
  status: string;
  outcome: string | null;
  changesRequested: boolean;
  agentId: string | null;
  agentInstanceId: string | null;
  agentRunId: string | null;
  conversationId: string | null;
  conversationUrl: string | null;
  waiting: boolean;
};

/** A step with the prose the agent wrote, which only its own page draws. */
export type ApiRunStep = ApiRunStepListing & {
  summary: string;
  outputs: { name: string; value: string }[];
};

/** One WorkOrder as `GET /api/runs` lists it.
 *
 *  Every screen polls that list once a second to keep the rail current, so it
 *  carries what a rail, a card and a milestone's task list read and no more.
 *  The prose an agent wrote is the WorkOrder page's, and comes with the single
 *  run it fetches. */
export type ApiWorkflowRunListing = {
  runId: string;
  name: string;
  workflowId: string;
  workflowName: string;
  workflowVersion: string;
  taskId: string;
  workstreamId: string | null;
  milestoneId: string | null;
  repository: string;
  repositoryContext: { repository: string };
  phase: string;
  currentStepId: string | null;
  terminalOutcome: string | null;
  steps: ApiRunStepListing[];
};

/** One whole WorkOrder, as `GET /api/runs/{runId}` answers for the page about
 *  it: the listing, and every word written along the way. */
export type ApiWorkflowRun = Omit<ApiWorkflowRunListing, "steps"> & {
  taskPrompt: string;
  failureReason: string;
  steps: ApiRunStep[];
  pendingHumanReview: {
    stepId: string;
    title: string;
    summary: string;
    prUrl: string | null;
  } | null;
  humanDecision: {
    stepId: string;
    approved: boolean;
    outcome: "approved" | "rejected";
    summary: string;
  } | null;
};

export type ApiGraphRun = {
  runId: string;
  graphId: string;
  status: "running" | "awaiting_approval" | "completed" | "failed";
  activeExecutions: { executionId: string; nodeId: string }[];
  nextNodes: string[];
  values: Record<string, unknown>;
  pendingApprovals: {
    approvalId: string;
    nodeId: string;
    reason: string;
    /** Only a request still open says what may be answered, which is why a
     *  conversation reads its open questions from here rather than from the
     *  event that raised them. */
    allowedDecisions: string[];
    kind?: string;
    command?: string;
    toolName?: string;
  }[];
  error: string;
};

export type ApiGraphTopology = {
  graphId: string;
  nodes: { nodeId: string; name: string; kind: string }[];
};

export type ApiGraphEvent = {
  sequence: number;
  type: string;
  nodeId: string | null;
  payload: Record<string, unknown>;
};

/** Everything the graph engine has said about a run so far.
 *
 *  A finite snapshot of the same feed `/graph/api/runs/{run}/events` streams,
 *  because a page that opens after an agent has finished still has to be able
 *  to read what it did. */
export function getGraphEvents(
  runId: string,
  signal?: AbortSignal,
): Promise<{ events: ApiGraphEvent[] }> {
  return api<{ events: ApiGraphEvent[] }>(
    `/api/runs/${encodeURIComponent(runId)}/graph-events`,
    { signal },
  );
}

export function getGraphRun(
  runId: string,
  signal?: AbortSignal,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(`/graph/api/runs/${encodeURIComponent(runId)}`, {
    signal,
  });
}

/** Say something to the agent a node is running, while it runs.
 *
 *  Addressed to the node rather than to the run: a graph may have several
 *  agents working at once, and the conversation on screen is one of them. The
 *  engine refuses this when that node has nothing in flight, because there is
 *  nobody to say it to -- steering is a message for a live turn, not a queued
 *  instruction for whatever runs next. */
export function steerGraphRun(
  runId: string,
  nodeId: string,
  message: string,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/steering`,
    { method: "POST", body: JSON.stringify({ message, node: nodeId }) },
  );
}

/** Answer a request the graph run stopped on, and get the run back as it left
 *  it: deciding is what releases the execution, so the two change together. */
export function decideGraphApproval(
  runId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
}

/** Record the decision a run stopped for, and get the finished run back.
 *
 *  The response is the whole run rather than the decision, because approving is
 *  the transition that ends it: the phase, the terminal outcome, and the human
 *  step all change together, and re-reading them separately would show a page
 *  half-decided. */
export function completeHumanReview(
  runId: string,
  approved: boolean,
  summary: string,
): Promise<ApiWorkflowRun> {
  return api<ApiWorkflowRun>(
    `/api/runs/${encodeURIComponent(runId)}/human-review`,
    {
      method: "POST",
      body: JSON.stringify({ approved, summary }),
    },
  );
}

/** Throw a WorkOrder away for good.
 *
 *  Unlike archiving a project there is nothing to restore afterwards: the run,
 *  its steps and its history go with it, which is why the rail asks first. */
export function deleteRun(runId: string): Promise<void> {
  return api<void>(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
  });
}

/** Choose the runner that answers this conversation from now on.
 *
 * Active workflow conversations restart their current turn on the new runner.
 */
export function setThreadRunner(
  threadId: string,
  runner: string,
): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify({ runner }),
  });
}

export function setThreadAutoApprove(
  threadId: string,
  autoApprove: boolean,
): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify({ autoApprove }),
  });
}

export function attachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, {
    method: "POST",
  });
}

export function detachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, {
    method: "DELETE",
  });
}

/** The three answers the product offers. A request may permit fewer: the
 *  provider decides what it can honour, and `allowedDecisions` says so. */
export type ApprovalDecision = "accept" | "accept_for_session" | "cancel";

/** One request for consent, exactly as the server persisted it.
 *
 *  Whole snapshots rather than diffs, because a browser that reconnects
 *  mid-pause has nothing to apply a diff to. */
export type ApiApproval = {
  id: string;
  status: "pending" | "decided" | "interrupted";
  kind:
    | "command_execution"
    | "file_change"
    | "tool_use"
    | "plan_approval"
    | "user_input";
  reason: string | null;
  command: string | null;
  cwd: string | null;
  toolName: string | null;
  /** The tool call this request is about, when the provider named one.
   *
   *  What lets the card sit beside the command it concerns. Null for a request
   *  the provider tied to no call, and for anything recorded before the pairing
   *  existed; those belong to the turn rather than to any one call in it. */
  toolCallId: string | null;
  arguments: string | null;
  allowedDecisions: ApprovalDecision[];
  decision: ApprovalDecision | null;
  decisionSource: "user" | "session_grant" | "policy" | null;
  questions?: ApiQuestion[];
  answers?: Record<string, string[]>;
};

export type ApiQuestion = {
  id: string;
  header: string;
  question: string;
  options: { label: string; description: string }[];
  multiSelect: boolean;
  allowsOther: boolean;
};

/** Answer the request this conversation's run is paused on.
 *
 *  Its own request rather than a reply on the stream that showed it: the
 *  connection that presented the pause may be long gone. */
export function decideApproval(
  threadId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<{ approval: ApiApproval }> {
  return api<{ approval: ApiApproval }>(
    `/api/threads/${threadId}/runs/current/approvals/${approvalId}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
}

export function answerQuestion(
  threadId: string,
  approvalId: string,
  answers: Record<string, string[]>,
): Promise<{ approval: ApiApproval }> {
  return api<{ approval: ApiApproval }>(
    `/api/threads/${threadId}/runs/current/approvals/${approvalId}`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}

/** Stop the run outright. The server cancels whatever it was waiting on first,
 *  which is the approval card's Cancel by another route. */
export function stopRun(threadId: string): Promise<void> {
  return api<void>(`/api/threads/${threadId}/runs/current`, {
    method: "DELETE",
  });
}

export type ApiMessage = {
  id: string;
  role: "user" | "assistant";
  content: ThreadMessage["content"];
};

export type ApiHistory = {
  messages: ApiMessage[];
  /** Everything this conversation has been asked to allow, oldest first. Sent
   *  with the transcript because it outlives the run that raised it. */
  approvals: ApiApproval[];
  unstable_resume: boolean;
};

export type GitHubStatus = { connected: boolean; clientIdConfigured: boolean };

export type SourceControlStatus = {
  provider: "gh-cli" | "github-oauth";
  autoSelected: boolean;
  ghCli: {
    installed: boolean;
    authenticated: boolean;
    account: string;
    message: string;
  };
};

export type SourceControlProviderStatus = {
  provider: "gh-cli" | "github-oauth";
  autoSelected: boolean;
};

export type GitHubClientIdInfo =
  | { source: "environment" | "configuration"; hint: string }
  | { source: "keychain"; hint: string }
  | { source: "none"; hint: "" };

export type GitHubConnectResponse = {
  userCode: string;
  verificationUri: string;
  expiresIn: number;
  interval: number;
};

export type GitHubPollResponse =
  | { status: "complete" }
  | {
      status: "pending";
      /** Seconds to wait before the next poll. Grows when GitHub returns slow_down. */
      nextInterval: number;
    };

export function getGitHubStatus(): Promise<GitHubStatus> {
  return api<GitHubStatus>("/api/github/status");
}

export function getSourceControlStatus(): Promise<SourceControlStatus> {
  return api<SourceControlStatus>("/api/source-control/status");
}

export function getSourceControlProvider(): Promise<SourceControlProviderStatus> {
  return api<SourceControlProviderStatus>("/api/source-control/provider");
}

export function setSourceControlProvider(
  provider: "gh-cli" | "github-oauth" | "gitlab",
): Promise<void> {
  return api<void>("/api/source-control/provider", {
    method: "POST",
    body: JSON.stringify({ provider }),
  });
}

export function getGitHubClientId(): Promise<GitHubClientIdInfo> {
  return api<GitHubClientIdInfo>("/api/github/client-id");
}

export function setGitHubClientId(clientId: string): Promise<void> {
  return api<void>("/api/github/client-id", {
    method: "POST",
    body: JSON.stringify({ clientId }),
  });
}

export function connectGitHub(): Promise<GitHubConnectResponse> {
  return api<GitHubConnectResponse>("/api/github/connect", { method: "POST" });
}

export function pollGitHubConnect(): Promise<GitHubPollResponse> {
  return api<GitHubPollResponse>("/api/github/connect/poll", {
    method: "POST",
  });
}

export function disconnectGitHub(): Promise<void> {
  return api<void>("/api/github/disconnect", { method: "POST" });
}

export type SlackStatus = { configured: boolean; connected: boolean };

export function getSlackStatus(): Promise<SlackStatus> {
  return api<SlackStatus>("/api/slack/status");
}

export function setSlackCredentials(clientId: string, clientSecret: string): Promise<void> {
  return api<void>("/api/slack/credentials", {
    method: "POST",
    body: JSON.stringify({ clientId, clientSecret }),
  });
}

export function connectSlack(): Promise<{ authorizationUrl: string }> {
  return api<{ authorizationUrl: string }>("/api/slack/connect", { method: "POST" });
}

export function disconnectSlack(): Promise<void> {
  return api<void>("/api/slack/disconnect", { method: "POST" });
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function messageText(message: { content: unknown }): string {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter(
      (part): part is { type: "text"; text: string } =>
        typeof part === "object" &&
        part !== null &&
        "type" in part &&
        part.type === "text" &&
        "text" in part &&
        typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("\n");
}
