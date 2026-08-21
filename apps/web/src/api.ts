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
  defaultRunner: string;
  workflowRunners: string[];
  defaultWorkflowRunner: string;
};

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

export type ApiRunStep = {
  stepId: string;
  name: string;
  kind: "agent" | "human";
  status: string;
  outcome: string | null;
  summary: string;
  outputs: { name: string; value: string }[];
  changesRequested: boolean;
  agentId: string | null;
  agentInstanceId: string | null;
  agentRunId: string | null;
  conversationId: string | null;
  conversationUrl: string | null;
  waiting: boolean;
};

export type ApiWorkflowRun = {
  runId: string;
  name: string;
  workflowId: string;
  workflowName: string;
  workflowVersion: string;
  taskId: string;
  taskPrompt: string;
  repository: string;
  repositoryContext: { repository: string };
  phase: string;
  currentStepId: string | null;
  terminalOutcome: string | null;
  failureReason: string;
  steps: ApiRunStep[];
  pendingHumanReview: { stepId: string; title: string; summary: string } | null;
  humanDecision: {
    stepId: string;
    approved: boolean;
    outcome: "approved" | "rejected";
    summary: string;
  } | null;
};

/** Answer the decision a run has stopped for, and get the finished run back.
 *
 *  Both answers are terminal — approving succeeds the run, rejecting fails it —
 *  so the response is the last state there is, and nothing is left to poll. */
export function completeHumanReview(
  runId: string,
  approved: boolean,
  summary: string,
): Promise<ApiWorkflowRun> {
  return api<ApiWorkflowRun>(`/api/runs/${encodeURIComponent(runId)}/human-review`, {
    method: "POST",
    body: JSON.stringify({ approved, summary }),
  });
}

/** Choose the runner that answers this conversation from now on. */
export function setThreadRunner(threadId: string, runner: string): Promise<ApiThread> {
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
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, { method: "POST" });
}

export function detachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, { method: "DELETE" });
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
  return api<void>(`/api/threads/${threadId}/runs/current`, { method: "DELETE" });
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
