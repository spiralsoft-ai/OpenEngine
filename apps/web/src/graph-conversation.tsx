/** A `[BETA]` WorkOrder's agent, read and steered as the conversation it is.
 *
 *  The graph engine does not keep a transcript. It keeps an event log -- what a
 *  node said, what it called, what it stopped to ask -- and this is the half
 *  that turns that log back into turns. Everything downstream of `messages` is
 *  the chat's own view: the same turns, the same folded tool rows, the same
 *  approval cards, the same styling. What a WorkOrder's agent is doing should
 *  not read differently from what a chat's agent is doing, because it is not
 *  different.
 *
 *  Three things are the graph engine's rather than a thread's, and they are the
 *  only three this file has to supply:
 *
 *  * the transcript is folded from events instead of loaded from a thread;
 *  * sending is **steering** -- a message for the turn an agent is in the
 *    middle of, refused when that node has nothing in flight, because there is
 *    nobody to say it to;
 *  * a request is answered on the graph engine's own approvals endpoint, which
 *    `answerApprovalsWith` is how the shared card learns.
 */

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import type { ReadonlyJSONObject, ReadonlyJSONValue } from "assistant-stream/utils";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  decideGraphApproval,
  getGraphEvents,
  getGraphRun,
  messageText,
  steerGraphRun,
  type ApiApproval,
  type ApiGraphEvent,
  type ApiGraphRun,
  type ApprovalDecision,
} from "./api";
import { answerApprovalsWith, publishApproval, type InlineApproval } from "./approvals";
import { ChatThread, ConversationStats } from "./chat";
import { phaseLabel } from "./runs";

/** What a node's conversation is called in the approvals store, and in the URL
 *  that leads back to it. Distinct from any thread id: nothing about it is a
 *  thread, and a collision would show one conversation's questions under
 *  another. */
export function graphConversationId(runId: string, nodeId: string): string {
  return `graph--${runId}--${nodeId}`;
}

export type GraphMessagePart =
  | { type: "text"; text: string }
  | {
      type: "tool-call";
      toolCallId: string;
      toolName: string;
      args: ReadonlyJSONObject;
      result?: ReadonlyJSONValue;
    };

/** One turn, in the shape assistant-ui reads with `convertMessage`. */
export type GraphMessage = {
  id: string;
  role: "user" | "assistant";
  content: GraphMessagePart[];
};

export type GraphConversation = {
  messages: GraphMessage[];
  /** Every request this node raised, with the turn it interrupted. */
  requests: InlineApproval[];
  /** Why the run stopped here, when it stopped here. */
  failure: string;
};

type Request = {
  approvalId: string;
  kind: string;
  reason: string;
  command: string;
  toolName: string;
  toolCallId: string;
  messageIndex: number;
};

const KINDS: readonly ApiApproval["kind"][] = [
  "command_execution",
  "file_change",
  "tool_use",
  "plan_approval",
  "user_input",
];

const DECISIONS: readonly ApprovalDecision[] = [
  "accept",
  "accept_for_session",
  "cancel",
];

/** What one node did, folded back into the turns it happened in.
 *
 *  Pure, and given the run's open requests rather than reading them, because
 *  only a request that is still open says what may be answered -- the event
 *  that raised it cannot know it has since been settled.
 */
export function graphConversation(
  events: readonly ApiGraphEvent[],
  pending: ApiGraphRun["pendingApprovals"] = [],
): GraphConversation {
  const messages: GraphMessage[] = [];
  const requests: Request[] = [];
  const calls = new Map<string, Extract<GraphMessagePart, { type: "tool-call" }>>();
  // A steering message is published twice: once when it is sent, and again when
  // the node picks it up. Counted, rather than remembered as a set, because
  // somebody may well say the same thing twice.
  const steered = new Map<string, number>();
  const resolved = new Map<string, string>();
  let turn: GraphMessage | undefined;
  let failure = "";

  for (const event of events) {
    if (event.type === "approval.resolved") {
      const id = String(event.payload.approvalId ?? "");
      if (id) resolved.set(id, String(event.payload.decision ?? ""));
    }
  }

  const assistantTurn = (sequence: number): GraphMessage => {
    if (!turn) {
      turn = { id: `assistant-${sequence}`, role: "assistant", content: [] };
      messages.push(turn);
    }
    return turn;
  };
  const userTurn = (sequence: number, text: string): void => {
    turn = undefined;
    messages.push({
      id: `user-${sequence}`,
      role: "user",
      content: [{ type: "text", text }],
    });
  };
  const call = (
    sequence: number,
    callId: string,
    toolName: string,
    args: ReadonlyJSONObject,
  ): void => {
    const known = calls.get(callId);
    if (known) {
      // The call the agent asked permission for, now that it is streaming it:
      // the same call, described by the agent rather than by its request.
      known.toolName = toolName || known.toolName;
      known.args = { ...known.args, ...args };
      return;
    }
    const part: Extract<GraphMessagePart, { type: "tool-call" }> = {
      type: "tool-call",
      toolCallId: callId,
      toolName,
      args,
    };
    calls.set(callId, part);
    assistantTurn(sequence).content.push(part);
  };

  for (const event of events) {
    const payload = event.payload;
    switch (event.type) {
      case "transcript": {
        const text = String(payload.text ?? "");
        if (!text) break;
        if (String(payload.role ?? "assistant") !== "user") {
          assistantTurn(event.sequence).content.push({ type: "text", text });
          break;
        }
        const shown = steered.get(text) ?? 0;
        // Already on screen from the moment it was sent. Showing it again
        // where the node happened to pick it up would read as somebody
        // repeating themselves.
        if (shown) {
          steered.set(text, shown - 1);
          break;
        }
        userTurn(event.sequence, text);
        break;
      }
      case "steering.received": {
        const text = String(payload.message ?? "");
        if (!text) break;
        steered.set(text, (steered.get(text) ?? 0) + 1);
        userTurn(event.sequence, text);
        break;
      }
      case "tool.call": {
        const callId = String(payload.callId ?? "");
        if (!callId) break;
        const args = payload.arguments;
        call(
          event.sequence,
          callId,
          String(payload.name ?? "") || "tool",
          // Read off JSON the server sent, which is the one place a cast is
          // the honest description of what is known about a value.
          args && typeof args === "object" && !Array.isArray(args)
            ? (args as ReadonlyJSONObject)
            : {},
        );
        break;
      }
      case "tool.result": {
        const known = calls.get(String(payload.callId ?? ""));
        if (known) known.result = payload.result as ReadonlyJSONValue;
        break;
      }
      case "approval.requested": {
        const approvalId = String(payload.approvalId ?? "");
        if (!approvalId) break;
        const callId = String(payload.toolCallId ?? "");
        const command = String(payload.command ?? "");
        const reason = String(payload.reason ?? "");
        const toolName = String(payload.toolName ?? "");
        // An agent asks before it streams the call, so the request is usually
        // what first tells the transcript there is a call at all. Standing one
        // up here is what puts the question under the command it is about for
        // the whole time the run is waiting on somebody, rather than only
        // afterwards.
        if (callId) {
          call(event.sequence, callId, toolName || "tool", {
            ...(command ? { command } : {}),
            ...(reason ? { reason } : {}),
          });
        }
        requests.push({
          approvalId,
          kind: String(payload.kind ?? ""),
          reason,
          command,
          toolName,
          toolCallId: callId,
          // The turn it interrupted, for a request that names no call and so
          // has nothing in the transcript to sit beside.
          messageIndex: turn ? messages.indexOf(turn) : messages.length,
        });
        break;
      }
      case "run.failed":
        failure = String(payload.error ?? "");
        break;
      default:
        break;
    }
  }

  const open = new Map(
    pending.map((approval) => [approval.approvalId, approval] as const),
  );
  return {
    messages,
    failure,
    requests: requests.map((request) => ({
      messageIndex: request.messageIndex,
      approval: approvalOf(request, open.get(request.approvalId), resolved),
    })),
  };
}

function approvalOf(
  request: Request,
  open: ApiGraphRun["pendingApprovals"][number] | undefined,
  resolved: Map<string, string>,
): ApiApproval {
  const decided = resolved.get(request.approvalId);
  const decision = DECISIONS.find((value) => value === decided) ?? null;
  return {
    id: request.approvalId,
    // Neither open nor answered means the execution that raised it is gone --
    // a cancelled run, or one whose superstep was refused out from under it.
    // Nobody can answer it now, and the card says so rather than offering
    // buttons that would be refused.
    status: open ? "pending" : decided ? "decided" : "interrupted",
    kind: KINDS.find((value) => value === request.kind) ?? "command_execution",
    reason: request.reason || null,
    command: request.command || null,
    cwd: null,
    toolName: request.toolName || null,
    toolCallId: request.toolCallId || null,
    arguments: null,
    allowedDecisions: (open?.allowedDecisions ?? []).flatMap((value) =>
      DECISIONS.filter((decision) => decision === value),
    ),
    decision,
    decisionSource: decision ? "user" : null,
  };
}

/** Everything the graph engine says about a run, kept current while a page is
 *  open. Both halves are needed and neither answers for the other: the feed is
 *  what happened, and the snapshot is what is true now -- which node is
 *  working, which questions are still open, where the checkout is. */
function useGraphRun(runId: string) {
  const [events, setEvents] = useState<ApiGraphEvent[]>([]);
  const [run, setRun] = useState<ApiGraphRun>();
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const [feed, snapshot] = await Promise.all([
          getGraphEvents(runId),
          // Tolerated, because what a reader came for is the transcript. A run
          // the engine no longer has a record of still has one here, and
          // failing the page over the snapshot would hide it -- there is
          // nothing to steer or answer on such a run anyway.
          getGraphRun(runId).catch(() => undefined),
        ]);
        if (cancelled) return;
        // Replaced only when it changed. The transcript is rebuilt from these,
        // and handing the view a fresh copy of the same events once a second
        // would redraw a conversation nobody has added to.
        setEvents((current) =>
          current.length === feed.events.length ? current : feed.events,
        );
        setRun((current) =>
          current && JSON.stringify(current) === JSON.stringify(snapshot)
            ? current
            : snapshot,
        );
        setError("");
        setLoaded(true);
      } catch (reason) {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setLoaded(true);
      } finally {
        if (!cancelled) timer = window.setTimeout(load, 1000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId, tick]);

  // Read again now rather than at the next poll, for the two moments where
  // waiting a second would look like the click did nothing.
  const refresh = useCallback(() => setTick((value) => value + 1), []);
  return { events, run, error, loaded, refresh };
}

const IDLE_NOTE =
  "Nothing is running here. A message steers an agent while it works, and " +
  "this node has none in flight to take one.";

function GraphComposer() {
  const aui = useAui();
  const canSend = useAuiState((state) => state.composer.canSend);
  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Say something to the agent working here…"
        aria-label="Message the agent"
        rows={1}
      />
      <button
        type="button"
        className="btn btn-primary"
        disabled={!canSend}
        onClick={() => aui.composer.send()}
      >
        Send
      </button>
    </ComposerPrimitive.Root>
  );
}

function GraphDock({
  working,
  workspace,
  error,
}: {
  working: boolean;
  workspace: string;
  error: string;
}) {
  return (
    <ThreadPrimitive.ViewportFooter className="dock">
      <ThreadPrimitive.ScrollToBottom className="btn jump-button">
        Jump to latest
      </ThreadPrimitive.ScrollToBottom>
      {working ? <GraphComposer /> : <p className="step-note">{IDLE_NOTE}</p>}
      {error && <p className="notice">{error}</p>}
      {workspace && (
        <div className="dock-foot">
          <div className="workspace-control">
            <span className="micro">Working in</span>
            <code className="dock-path">cd {workspace}</code>
          </div>
        </div>
      )}
    </ThreadPrimitive.ViewportFooter>
  );
}

export function GraphConversationPage({
  runId,
  nodeId,
}: {
  runId: string;
  nodeId: string;
}) {
  const { events, run, error, loaded, refresh } = useGraphRun(runId);
  const [steerError, setSteerError] = useState("");
  const conversationId = graphConversationId(runId, nodeId);

  const nodeEvents = useMemo(
    () => events.filter((event) => event.nodeId === nodeId),
    [events, nodeId],
  );
  const pending = useMemo(
    () =>
      (run?.pendingApprovals ?? []).filter((approval) => approval.nodeId === nodeId),
    [run, nodeId],
  );
  const conversation = useMemo(
    () => graphConversation(nodeEvents, pending),
    [nodeEvents, pending],
  );
  const working = Boolean(
    run?.activeExecutions.some((execution) => execution.nodeId === nodeId),
  );
  const workspace =
    typeof run?.values.workspace === "string" ? run.values.workspace : "";

  useEffect(() => {
    for (const entry of conversation.requests)
      publishApproval(conversationId, entry.approval, entry.messageIndex);
  }, [conversation, conversationId]);

  useEffect(
    () =>
      answerApprovalsWith(conversationId, {
        decide: async (approvalId, decision) => {
          const decided = await decideGraphApproval(runId, approvalId, decision);
          refresh();
          return decided;
        },
        // A graph request carries no questions, so nothing renders the form
        // that would call this. Refusing loudly beats answering a question
        // nobody asked.
        answer: () =>
          Promise.reject(
            new Error("This request is answered with a decision, not an answer."),
          ),
      }),
    [conversationId, refresh, runId],
  );

  const convertMessage = useCallback(
    (message: GraphMessage, index: number): ThreadMessageLike => ({
      id: message.id,
      role: message.role,
      content: message.content,
      // Only the turn a failure ended, and only when one did. Everything else
      // takes the status assistant-ui derives from `isRunning`, which is what
      // makes a call the agent is still making read as one.
      ...(message.role === "assistant" &&
      index === conversation.messages.length - 1 &&
      conversation.failure
        ? {
            status: {
              type: "incomplete" as const,
              reason: "error" as const,
              error: conversation.failure,
            },
          }
        : {}),
    }),
    [conversation],
  );

  const threads = useMemo(
    () => [
      {
        id: conversationId,
        remoteId: conversationId,
        status: "regular" as const,
        title: phaseLabel(nodeId),
        // What the shared view reads to know whose conversation this is: the
        // backlink to the WorkOrder is built from these two.
        custom: { workflowRunId: runId, workflowStepId: nodeId, editable: working },
      },
    ],
    [conversationId, nodeId, runId, working],
  );

  const runtime = useExternalStoreRuntime<GraphMessage>({
    isLoading: !loaded,
    isRunning: working,
    isSendDisabled: !working,
    messages: conversation.messages,
    convertMessage,
    onNew: async (message) => {
      const text = messageText(message).trim();
      if (!text) return;
      setSteerError("");
      try {
        await steerGraphRun(runId, nodeId, text);
      } catch (failure) {
        setSteerError(failure instanceof Error ? failure.message : String(failure));
      }
      refresh();
    },
    adapters: { threadList: { threadId: conversationId, threads } },
  });

  return (
    <main className="panel">
      <header className="panel-head panel-head-workflow">
        <div className="panel-head-copy">
          <p className="eyebrow">WorkOrder conversation</p>
          <h1>{phaseLabel(nodeId)}</h1>
          <p className="lede">
            {working
              ? "This node's agent is working. What you send reaches the turn it is in the middle of."
              : "A WorkOrder node owns this transcript."}
          </p>
        </div>
      </header>
      {error && <p className="notice notice-block">{error}</p>}
      <AssistantRuntimeProvider runtime={runtime}>
        <ConversationStats />
        <ChatThread
          empty={
            <p className="state-inline">
              {loaded ? "Waiting for agent activity…" : "Loading conversation…"}
            </p>
          }
          dock={
            <GraphDock working={working} workspace={workspace} error={steerError} />
          }
        />
      </AssistantRuntimeProvider>
    </main>
  );
}
