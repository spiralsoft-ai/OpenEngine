import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  QueueItemPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useToolCallElapsed,
} from "@assistant-ui/react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import {
  messageText,
  RUN_NOT_STARTED_ERROR_CODE,
  stopRun,
  type ApiApproval,
  type ApprovalDecision,
} from "./api";
import { approvalActions, useApprovals, type InlineApproval } from "./approvals";
import { Stat, StatStrip } from "./brand";
import { WorkspaceControl } from "./workspace";

const COMPOSER_DRAFT_KEY_PREFIX = "engine.composerDraft.";
const COMPOSER_QUEUE_KEY_PREFIX = "engine.composerQueue.";
const NEW_CHAT_DRAFT_ID = "new";

function readQueuedMessages(key: string): string[] {
  const saved = window.localStorage.getItem(key);
  if (!saved) return [];
  try {
    const messages = JSON.parse(saved) as unknown;
    if (!Array.isArray(messages)) return [];
    return messages.filter(
      (message): message is string => typeof message === "string" && message.length > 0,
    );
  } catch {
    return [];
  }
}

export function toolResultText(result: unknown): string {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2) ?? String(result);
  } catch {
    return String(result);
  }
}

/** The argument worth putting on the folded line: the thing the call is about.
 *
 *  A row that says only "ran Bash" makes the reader open every one of them to
 *  find the call they remember. The command, or the file, is what they are
 *  scanning for, so it goes where scanning finds it. */
const DETAIL_KEYS = ["command", "file_path", "path", "pattern", "query", "url", "notebook_path"];

export function toolDetail(args: unknown): string {
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const record = args as Record<string, unknown>;
  for (const key of DETAIL_KEYS) {
    const value = record[key];
    if (typeof value !== "string" || !value.trim()) continue;
    const line = value.trim().split("\n")[0];
    return line.length > 72 ? `${line.slice(0, 71)}…` : line;
  }
  return "";
}

/** How long the call took, once that is a fact.
 *
 *  Only a run this browser watched carries timing; a transcript loaded from the
 *  server does not, and the hook says so by returning nothing. An empty right
 *  edge is the honest answer there. */
function ToolElapsed() {
  const elapsed = useToolCallElapsed();
  if (elapsed === undefined) return null;
  const seconds = elapsed / 1000;
  return <span>{seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s</span>;
}

function ToolCall({
  toolName,
  args,
  argsText,
  result,
  running,
}: {
  toolName: string;
  args: unknown;
  argsText: string;
  result: unknown;
  running: boolean;
}) {
  const detail = toolDetail(args);
  return (
    <details className="tool" data-live={running || undefined}>
      <summary className="tool-head">
        <span className="tool-name">
          <span className="tool-caret" aria-hidden="true">
            ▸
          </span>
          <span className="tool-title">
            {running ? "running" : "ran"} {toolName}
          </span>
          {detail && <span className="tool-detail">{detail}</span>}
        </span>
        <span className="tool-meta">
          <ToolElapsed />
          {running && <span className="tool-live">LIVE</span>}
        </span>
      </summary>
      <pre>{argsText || JSON.stringify(args, null, 2)}</pre>
      {result !== undefined && <pre className="tool-result">{toolResultText(result)}</pre>}
    </details>
  );
}

function TextParts() {
  return (
    <MessagePrimitive.Parts>
      {({ part }) =>
        part.type === "text" ? (
          <MessagePartPrimitive.Text component="p" />
        ) : part.type === "tool-call" ? (
          <>
            <ToolCall
              toolName={part.toolName}
              args={part.args}
              argsText={part.argsText}
              result={part.result}
              running={part.status.type === "running"}
            />
            {/* Under the call it was asked about, because that is what it is
                about. A turn that ran three commands and stopped to ask about
                the second one reads that way, and whatever the agent said
                afterwards stays below the question it waited on. */}
            <CallApprovals toolCallId={part.toolCallId} />
          </>
        ) : null
      }
    </MessagePrimitive.Parts>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message message-user">
      <TextParts />
    </MessagePrimitive.Root>
  );
}

/** What a failed run has to say for itself.
 *
 *  assistant-ui does not keep the thrown Error: `toAssistantError` normalizes
 *  it to a plain `{code, message}`, so an `instanceof Error` test never matches
 *  and stringifying the object prints "[object Object]" over the one sentence
 *  the reader needed. Read the message wherever it ended up.
 */
export function errorText(error: unknown): string {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && "message" in error) {
    const { message } = error as { message: unknown };
    if (typeof message === "string") return message;
  }
  return "The agent run failed.";
}

export function AssistantMessage() {
  const error = useAuiState((state) => {
    const status = state.message.status;
    if (status?.type !== "incomplete" || status.reason !== "error") return undefined;
    return status.error;
  });

  return (
    <MessagePrimitive.Root className="message message-assistant">
      <TextParts />
      <TurnApprovals />
      <MessagePrimitive.Error>
        <p className="notice message-error">
          <Ticked text={errorText(error)} />
        </p>
      </MessagePrimitive.Error>
    </MessagePrimitive.Root>
  );
}

/** Keep pending follow-ups through the full page loads used for navigation.
 *
 *  The queue belongs to assistant-ui's in-memory runtime. Wait for history to
 *  settle before putting saved messages back: at that point an active run has
 *  resumed and they queue behind it, or a run that finished while this page
 *  was away accepts the first follow-up immediately. */
export function QueuedMessagePersistence({ draftRestored }: { draftRestored: boolean }) {
  const aui = useAui();
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const historyLoading = useAuiState((state) => state.thread.isLoading);
  const canSend = useAuiState((state) => state.composer.canSend);
  const queue = useAuiState((state) => state.composer.queue);
  const queueKey = remoteId ? `${COMPOSER_QUEUE_KEY_PREFIX}${remoteId}` : undefined;
  const [restoredQueueKey, setRestoredQueueKey] = useState<string>();
  const [restoringQueue, setRestoringQueue] = useState<{
    key: string;
    messages: string[];
    index: number;
    draft: string;
  }>();

  useEffect(() => {
    if (!queueKey || historyLoading || !draftRestored) return;

    const saved = readQueuedMessages(queueKey);
    if (saved.length && aui.composer.getState().queue.length === 0) {
      const draft = aui.composer.getState().text;
      aui.composer.setText(saved[0]!);
      setRestoringQueue({ key: queueKey, messages: saved, index: 0, draft });
      return;
    }
    setRestoringQueue(undefined);
    setRestoredQueueKey(queueKey);
  }, [aui, draftRestored, historyLoading, queueKey]);

  useEffect(() => {
    if (!restoringQueue || restoringQueue.key !== queueKey || !canSend) return;
    aui.composer.send();
    const index = restoringQueue.index + 1;
    const next = restoringQueue.messages[index];
    if (next !== undefined) {
      aui.composer.setText(next);
      setRestoringQueue({ ...restoringQueue, index });
      return;
    }
    aui.composer.setText(restoringQueue.draft);
    setRestoringQueue(undefined);
    setRestoredQueueKey(restoringQueue.key);
  }, [aui, canSend, queueKey, restoringQueue]);

  useEffect(() => {
    if (!queueKey || restoredQueueKey !== queueKey) return;
    const messages = queue
      .map((item) =>
        item.parts
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join("\n\n"),
      )
      .filter((message) => message.length > 0);
    if (messages.length) window.localStorage.setItem(queueKey, JSON.stringify(messages));
    else window.localStorage.removeItem(queueKey);
  }, [queue, queueKey, restoredQueueKey]);

  return null;
}

export function Composer({ project = false }: { project?: boolean } = {}) {
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const canSend = useAuiState((state) => state.composer.canSend);
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const text = useAuiState((state) => state.composer.text);
  const messages = useAuiState((state) => state.thread.messages);
  const draftKey = `${COMPOSER_DRAFT_KEY_PREFIX}${remoteId ?? NEW_CHAT_DRAFT_ID}`;
  const [restoredDraftKey, setRestoredDraftKey] = useState<string>();
  const restoredFailure = useRef<string | undefined>(undefined);

  useEffect(() => {
    const savedDraft = window.localStorage.getItem(draftKey) ?? "";
    if (aui.composer.getState().text !== savedDraft) {
      aui.composer.setText(savedDraft);
    }
    setRestoredDraftKey(draftKey);
  }, [aui, draftKey]);

  useEffect(() => {
    if (restoredDraftKey !== draftKey) return;
    if (text) window.localStorage.setItem(draftKey, text);
    else window.localStorage.removeItem(draftKey);
  }, [draftKey, restoredDraftKey, text]);

  useEffect(() => {
    // A pre-stream failure means the server never stored this user message,
    // so put it back where it can be corrected or sent again.
    const failed = messages.at(-1);
    const submitted = messages.at(-2);
    if (
      failed?.role !== "assistant" ||
      failed.status.type !== "incomplete" ||
      failed.status.reason !== "error" ||
      !failed.status.error ||
      typeof failed.status.error !== "object" ||
      !("code" in failed.status.error) ||
      failed.status.error.code !== RUN_NOT_STARTED_ERROR_CODE ||
      submitted?.role !== "user"
    ) {
      return;
    }

    const failureKey = `${draftKey}:${failed.id}`;
    if (restoredFailure.current === failureKey) return;
    restoredFailure.current = failureKey;

    const submittedText = messageText(submitted);
    if (submittedText && !aui.composer.getState().text) {
      aui.composer.setText(submittedText);
    }
  }, [aui, draftKey, messages]);

  const send = () => {
    aui.composer.send();
    window.localStorage.removeItem(draftKey);
  };

  const queueOnEnter = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      !isRunning ||
      !canSend ||
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing
    ) {
      return;
    }
    event.preventDefault();
    send();
  };

  return (
    <>
      <QueuedMessagePersistence draftRestored={restoredDraftKey === draftKey} />
      <div className="queue" aria-live="polite">
        <ComposerPrimitive.Queue>
          {() => (
            <div className="queued">
              <span className="queued-label">Queued</span>
              <QueueItemPrimitive.Text className="queued-text" />
              <QueueItemPrimitive.Remove
                className="queued-remove"
                aria-label="Remove queued message"
                title="Remove queued message"
              >
                ×
              </QueueItemPrimitive.Remove>
            </div>
          )}
        </ComposerPrimitive.Queue>
      </div>
      <ComposerPrimitive.Root
        className="composer"
        onSubmit={() => window.localStorage.removeItem(draftKey)}
      >
        <ComposerPrimitive.Input
          className="composer-input"
          placeholder={
            isRunning
              ? "Queue a message for when the agent is done…"
              : project
                ? "Tell the agent about the project you're working on.."
                : "Ask the agent about this repository…"
          }
          aria-label="Message the agent"
          rows={1}
          onKeyDown={queueOnEnter}
        />
        <ComposerPrimitive.Cancel
          className="btn"
          onClick={(event) => {
            const { remoteId } = aui.threadListItem.getState();
            if (!remoteId) return;

            // Stopping a run that is waiting on an approval goes through the
            // same cancel the card's own button does: the server records the
            // request as cancelled and hands that to the provider, so the
            // action does not run, and only then tears the turn down.
            const stop = () => stopRun(remoteId).catch(() => {});

            const queued = aui.composer.getState().queue[0];
            if (!queued) {
              void stop();
              return;
            }

            // Let the server finish cancelling before starting the follow-up.
            // Preventing the primitive's local cancel lets the queue drain as
            // soon as the active stream closes.
            event.preventDefault();
            void stop().then(() => {
              if (aui.composer.getState().queue.some((item) => item.id === queued.id)) {
                aui.composer.queueItem({ id: queued.id }).move({ lane: "steer" });
              }
            });
          }}
        >
          Stop
        </ComposerPrimitive.Cancel>
        <button type="button" className="btn btn-primary" disabled={!canSend} onClick={send}>
          {isRunning ? "Queue" : "Send"}
        </button>
      </ComposerPrimitive.Root>
    </>
  );
}

type WorkspaceCustom = {
  workspaceRoot?: string;
  workspaceRef?: string;
  workspaceAttached?: boolean;
  workflowRunId?: string;
  workflowStepId?: string;
  editable?: boolean;
};

function WorkflowBacklink() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  if (!custom?.workflowRunId) return null;
  return (
    <a className="backlink" href={`/runs/${custom.workflowRunId}`}>
      ← Back to WorkOrder {custom.workflowRunId}
      {custom.workflowStepId && <> · {custom.workflowStepId} step</>}
    </a>
  );
}

/** Server messages mark the command you are meant to run in backticks; show
 *  that span as the code it is, so it can be read and copied as one thing. */
function Ticked({ text }: { text: string }) {
  return (
    <>
      {text.split(/`([^`]+)`/).map((part, index) =>
        index % 2 ? <code key={index}>{part}</code> : part,
      )}
    </>
  );
}

/** Adapt the open assistant-ui conversation to the shared checkout control. */
function WorkspaceLine() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  if (!remoteId) return null;
  return <WorkspaceControl threadId={remoteId} initial={custom} />;
}

const DECISION_LABELS: Record<ApprovalDecision, string> = {
  accept: "Approve",
  accept_for_session: "Allow similar actions for this session",
  cancel: "Cancel",
};

const KIND_LABELS: Record<ApiApproval["kind"], string> = {
  command_execution: "Wants to run a command",
  file_change: "Wants to change files",
  tool_use: "Wants to use a tool",
  plan_approval: "Wants approval for a plan",
  user_input: "Has a question",
};

/** Whether this request is a form to fill in rather than a yes or no.
 *
 *  The questions decide, not the kind. A provider states what it wants an
 *  answer to; a runtime may raise a request for a person under the same kind
 *  and put no questions on it -- a graph's human-review node asks for a verdict
 *  that way. Rendering a question form for one would present an empty dialog
 *  with nothing to submit, in front of the only control that would move the run
 *  on. */
function asksQuestions(approval: ApiApproval): boolean {
  return approval.kind === "user_input" && Boolean(approval.questions?.length);
}

/** What became of a request that is no longer open, and on whose say-so. */
export function outcomeText(approval: ApiApproval): string {
  if (approval.status === "interrupted")
    return "Interrupted — the agent that asked this is gone, so it can no longer be answered.";
  if (approval.decisionSource === "session_grant")
    return "Approved automatically: you allowed this exact action for this conversation earlier.";
  if (approval.decisionSource === "policy")
    return approval.decision === "cancel"
      ? "Refused by the configured policy — the action did not run."
      : "Allowed by the configured policy, without asking.";
  if (asksQuestions(approval) && approval.answers) return "Answered.";
  if (asksQuestions(approval) && approval.decision === "cancel")
    return "Cancelled — no answer was sent.";
  switch (approval.decision) {
    case "accept":
      return "Approved.";
    case "accept_for_session":
      return "Approved, and allowed again for this conversation without asking.";
    case "cancel":
      return "Cancelled — the action did not run.";
    default:
      return "This request is no longer open.";
  }
}

/** The request's own arguments, shown as fields when they are fields.
 *
 *  Structured rather than dumped: the point of the card is that somebody can
 *  tell what they are agreeing to, and a wall of JSON is read by nobody. What
 *  will not parse is still shown -- unreadable is better than hidden. */
function ApprovalArguments({ approval }: { approval: ApiApproval }) {
  if (!approval.arguments) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(approval.arguments);
  } catch {
    return <pre className="approval-arguments">{approval.arguments}</pre>;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    return <pre className="approval-arguments">{JSON.stringify(parsed, null, 2)}</pre>;

  const fields = Object.entries(parsed as Record<string, unknown>).filter(
    ([key, value]) =>
      value !== null &&
      value !== undefined &&
      // The command already has a line of its own above.
      !(key === "command" && value === approval.command),
  );
  if (!fields.length) return null;

  return (
    <dl className="approval-arguments">
      {fields.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The one line a folded approval is worth: what happened, and to what. */
export function summaryText(approval: ApiApproval): string {
  if (asksQuestions(approval)) {
    const question = approval.questions?.[0]?.question ?? "Question";
    if (approval.status === "pending") return `Answer needed · ${question}`;
    return approval.decision === "cancel"
      ? `Cancelled · ${question}`
      : `Answered · ${question}`;
  }
  const target =
    approval.command ?? approval.toolName ?? KIND_LABELS[approval.kind].toLowerCase();
  if (approval.status === "pending") return `Approval needed · ${target}`;
  if (approval.status === "interrupted") return `Interrupted · ${target}`;
  if (approval.decisionSource === "session_grant")
    return `Approved automatically · ${target}`;
  if (approval.decisionSource === "policy")
    return approval.decision === "cancel"
      ? `Refused by policy · ${target}`
      : `Allowed by policy · ${target}`;
  switch (approval.decision) {
    case "accept":
      return `Approved · ${target}`;
    case "accept_for_session":
      return `Approved for this conversation · ${target}`;
    case "cancel":
      return `Cancelled · ${target}`;
    default:
      return `Closed · ${target}`;
  }
}

function QuestionForm({
  threadId,
  approval,
}: {
  threadId: string;
  approval: ApiApproval;
}) {
  const questions = approval.questions ?? [];
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  function choose(questionId: string, label: string, multiSelect: boolean) {
    setSelected((current) => {
      const existing = current[questionId] ?? [];
      return {
        ...current,
        [questionId]: multiSelect
          ? existing.includes(label)
            ? existing.filter((value) => value !== label)
            : [...existing, label]
          : [label],
      };
    });
  }

  async function submit() {
    const answers: Record<string, string[]> = {};
    for (const question of questions) {
      const typed = (other[question.id] ?? "").trim();
      const choices = selected[question.id] ?? [];
      answers[question.id] = question.multiSelect
        ? [...choices, ...(typed ? [typed] : [])]
        : typed
          ? [typed]
          : choices;
      if (!answers[question.id].length) {
        setError(`Answer “${question.header}” before continuing.`);
        return;
      }
    }
    setBusy(true);
    setError(undefined);
    try {
      await approvalActions(threadId).answer(approval.id, answers);
    } catch (failure) {
      setBusy(false);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  async function cancel() {
    setBusy(true);
    setError(undefined);
    try {
      await approvalActions(threadId).decide(approval.id, "cancel");
    } catch (failure) {
      setBusy(false);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <div className="question-backdrop">
      <section
        className="question-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Agent question"
      >
        <header>
          <span className="approval-kind">The agent needs your input</span>
        </header>
        {questions.map((question) => (
          <fieldset className="question-field" key={question.id}>
            <legend>{question.header}</legend>
            <p>{question.question}</p>
            <div className="question-options">
              {question.options.map((option) => {
                const checked = (selected[question.id] ?? []).includes(option.label);
                return (
                  <label
                    className="question-option"
                    key={option.label}
                    data-selected={checked || undefined}
                  >
                    <input
                      type={question.multiSelect ? "checkbox" : "radio"}
                      name={question.id}
                      checked={checked}
                      onChange={() =>
                        choose(question.id, option.label, question.multiSelect)
                      }
                    />
                    <span>
                      <strong>{option.label}</strong>
                      {option.description && <small>{option.description}</small>}
                    </span>
                  </label>
                );
              })}
            </div>
            {question.allowsOther && (
              <label className="question-other">
                Other
                <input
                  value={other[question.id] ?? ""}
                  onChange={(event) =>
                    setOther((current) => ({
                      ...current,
                      [question.id]: event.target.value,
                    }))
                  }
                  placeholder="Type your answer…"
                />
              </label>
            )}
          </fieldset>
        ))}
        {error && <p className="notice">{error}</p>}
        <div className="approval-actions">
          {approval.allowedDecisions.includes("cancel") && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => void cancel()}
            >
              {busy ? "Sending…" : "Cancel"}
            </button>
          )}
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !questions.length}
            onClick={() => void submit()}
          >
            {busy ? "Sending…" : "Continue"}
          </button>
        </div>
      </section>
    </div>
  );
}

/** What the turn is asking, and the answers this request permits.
 *
 *  Only those answers: a provider that never offered a session grant cannot
 *  honour one, so offering the button would be offering something we would
 *  have to refuse.
 *
 *  A `details` rather than a panel, because this outlives the question. While
 *  it is pending it is the only thing worth reading and is open; once it is
 *  answered it folds down to a line in the transcript beside the command it
 *  was about, where it is a record rather than a demand. */
export function ApprovalEntry({
  threadId,
  approval,
}: {
  threadId: string;
  approval: ApiApproval;
}) {
  const pending = approval.status === "pending";
  const [open, setOpen] = useState(pending);
  const [submitted, setSubmitted] = useState<ApprovalDecision>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    // Answered, so it stops being the thing under your eyes. Reopening is one
    // click, and a manual reopen survives because this only fires on the
    // transition.
    setOpen(pending);
  }, [pending]);

  async function decide(decision: ApprovalDecision) {
    setSubmitted(decision);
    setError(undefined);
    try {
      await approvalActions(threadId).decide(approval.id, decision);
    } catch (failure) {
      // Stale, already answered, or a provider that has since died. The
      // decision did not land, so the controls come back with the reason.
      setSubmitted(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <details
      className={`approval approval-${pending ? "pending" : approval.status}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="approval-summary">{summaryText(approval)}</summary>
      <div className="approval-body" aria-live="polite">
        <header className="approval-head">
          <span className="approval-kind">{KIND_LABELS[approval.kind]}</span>
          <span className="approval-id">{approval.id}</span>
        </header>
        {approval.reason && <p className="approval-reason">{approval.reason}</p>}
        {approval.command && <pre className="approval-command">{approval.command}</pre>}
        {(approval.toolName || approval.cwd) && (
          <dl className="approval-facts">
            {approval.toolName && (
              <div>
                <dt>Tool</dt>
                <dd>{approval.toolName}</dd>
              </div>
            )}
            {approval.cwd && (
              <div>
                <dt>Working directory</dt>
                <dd>{approval.cwd}</dd>
              </div>
            )}
          </dl>
        )}
        <ApprovalArguments approval={approval} />
        {pending && asksQuestions(approval) && (
          <QuestionForm threadId={threadId} approval={approval} />
        )}
        {pending && !asksQuestions(approval) ? (
          <div className="approval-actions">
            {approval.allowedDecisions.map((decision) => (
              <button
                key={decision}
                type="button"
                className={`btn ${decision === "cancel" ? "" : "btn-primary"}`}
                // Disabled the instant one is chosen: a second click is a second
                // decision, and the server refuses those rather than applying
                // them to whatever is running by then.
                disabled={submitted !== undefined}
                onClick={() => void decide(decision)}
              >
                {submitted === decision ? "Sending…" : DECISION_LABELS[decision]}
              </button>
            ))}
          </div>
        ) : !pending ? (
          <p className="approval-outcome">{outcomeText(approval)}</p>
        ) : null}
        {error && (
          <p className="notice">
            <Ticked text={error} />
          </p>
        )}
      </div>
    </details>
  );
}

function ApprovalList({
  threadId,
  entries,
  className,
}: {
  threadId: string;
  entries: readonly InlineApproval[];
  className: string;
}) {
  if (!entries.length) return null;
  return (
    <div className={className}>
      {entries.map(({ approval }) => (
        <ApprovalEntry key={approval.id} threadId={threadId} approval={approval} />
      ))}
    </div>
  );
}

/** What this conversation was asked to allow before one particular call.
 *
 *  Matched by the call's id rather than by where the request happened to arrive
 *  on the stream, so the pairing is the one the provider stated and survives a
 *  reload, a reconnect, and a turn that asked about only some of its calls. */
function CallApprovals({ toolCallId }: { toolCallId: string }) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const approvals = useApprovals(remoteId);
  const mine = useMemo(
    () => approvals.filter((entry) => entry.approval.toolCallId === toolCallId),
    [approvals, toolCallId],
  );

  if (!remoteId) return null;
  return (
    <ApprovalList
      threadId={remoteId}
      entries={mine}
      className="approvals approvals-call"
    />
  );
}

/** What this turn is still waiting on that names no call at all.
 *
 *  A provider may pause over something that is not a tool call -- a question
 *  put to the reader, a request the record carries no call id for. There is no
 *  call for those to sit beside, and leaving them unrendered would leave the
 *  run waiting on an answer nobody can see to give. So while one is pending it
 *  is shown at the end of the turn that raised it, anchored by index so it
 *  stays with that turn once the conversation has moved on.
 *
 *  Only while it is pending. Answered, it is a result with nothing to sit
 *  beside, and a result stacked at the end of the page is the thing this slot
 *  exists not to be: it reads as a comment on whatever the turn did last. The
 *  card disappears when the run stops needing it.
 *
 *  Only the ones naming no call, too. A request that names one belongs beside
 *  that call and nowhere else: if the transcript does not hold the call, the
 *  request is not shown. */
function TurnApprovals() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const index = useAuiState((state) => state.message.index);
  const approvals = useApprovals(remoteId);
  const mine = useMemo(
    () =>
      approvals.filter(
        (entry) =>
          entry.messageIndex === index &&
          !entry.approval.toolCallId &&
          entry.approval.status === "pending",
      ),
    [approvals, index],
  );

  if (!remoteId) return null;
  return <ApprovalList threadId={remoteId} entries={mine} className="approvals" />;
}

/** The line of figures under the conversation heading.
 *
 *  Every cell is counted from the transcript this browser is holding, so each
 *  one is a fact about what is on screen rather than an estimate of anything. */
export function ConversationStats() {
  const messages = useAuiState((state) => state.thread.messages);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const approvals = useApprovals(remoteId);

  const toolCalls = useMemo(
    () =>
      messages.reduce(
        (total, message) =>
          total +
          (Array.isArray(message.content)
            ? message.content.filter((part) => part.type === "tool-call").length
            : 0),
        0,
      ),
    [messages],
  );
  const pending = approvals.filter((entry) => entry.approval.status === "pending").length;

  return (
    <StatStrip>
      <Stat label="Turns" value={messages.length} />
      <Stat label="Tool calls" value={toolCalls} />
      <Stat
        label="Approvals"
        value={pending ? `${approvals.length} · ${pending} open` : approvals.length}
        tone={pending ? "alert" : undefined}
      />
      <Stat
        label="Status"
        value={isRunning ? "Live" : "Idle"}
        tone={isRunning ? "live" : undefined}
      />
    </StatStrip>
  );
}

/** The transcript, and the controls under it.
 *
 *  `empty` and `dock` are what a conversation that is not a chat replaces. Both
 *  halves of the difference are there: what an empty one says, and what sending
 *  means. Everything between them -- the turns, the folded calls, the approval
 *  cards -- is the same view whichever engine is answering, which is the point:
 *  a WorkOrder's agent reads as the conversation it is rather than as a second
 *  rendering of one. */
export function ChatThread({
  project = false,
  empty,
  dock,
}: {
  project?: boolean;
  empty?: ReactNode;
  dock?: ReactNode;
}) {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="stream">
        <WorkflowBacklink />
        <ThreadPrimitive.Empty>
          {empty ??
            (!project && (
              <div className="welcome">
                <div className="welcome-copy">
                  <p className="eyebrow">OpenEngine / Chat</p>
                  <h1>Start a conversation.</h1>
                  <p className="lede">Each chat has its own agent history and Git worktree.</p>
                </div>
              </div>
            ))}
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <UserMessage /> : <AssistantMessage />
          }
        </ThreadPrimitive.Messages>
        {dock ?? <Dock project={project} />}
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

export const READ_ONLY_WORKORDER_NOTE =
  "This transcript belongs to a WorkOrder step. Return to the WorkOrder for status and actions.";

function Dock({ project }: { project: boolean }) {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  const workflowConversation = Boolean(custom?.workflowRunId);
  const editable = !workflowConversation || Boolean(custom?.editable);
  return (
    <ThreadPrimitive.ViewportFooter className="dock">
      <ThreadPrimitive.ScrollToBottom className="btn jump-button">
        Jump to latest
      </ThreadPrimitive.ScrollToBottom>
      {!editable ? (
        <p className="step-note">{READ_ONLY_WORKORDER_NOTE}</p>
      ) : (
        <Composer project={project} />
      )}
      <div className="dock-foot">
        {/* Under the composer or workflow note rather than in the heading: a
            detached conversation refuses to run, so the way to fix that cannot
            be somewhere you have to scroll a long transcript to reach. */}
        <WorkspaceLine />
      </div>
    </ThreadPrimitive.ViewportFooter>
  );
}
