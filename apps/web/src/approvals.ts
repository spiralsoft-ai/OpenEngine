/** The approvals a conversation has raised, in the order it raised them.
 *
 *  An approval arrives interleaved with message content on the run stream, but
 *  it is not message content: it is the state of the turn, it outlives the
 *  connection that carried it, and it has to be readable from a component that
 *  is nowhere near the code reading the stream. So it is kept here, beside
 *  assistant-ui's message state rather than inside it, and read through
 *  `useSyncExternalStore` so a card re-renders the moment a snapshot lands.
 *
 *  Kept as a list rather than as "the current one", because an answered request
 *  is part of what happened in a turn: what the agent wanted to do, and what
 *  somebody said about it, belong in the transcript next to the command they
 *  are about. A turn may also raise several.
 *
 *  Keyed by thread because runs are: several conversations stream at once, and
 *  a pause in one must not appear under another.
 */

import { useCallback, useSyncExternalStore } from "react";

import {
  answerQuestion,
  decideApproval,
  type ApiApproval,
  type ApprovalDecision,
} from "./api";

export type InlineApproval = {
  /** The assistant turn that raised it, as an index into the thread.
   *
   *  Recorded when the request first arrives and never moved afterwards: the
   *  same request is republished as it is decided, and by then the turn it
   *  interrupted may no longer be the last one.
   *
   *  What places a request that names no tool call while it is pending, and
   *  nothing else. One that names a call is shown beside that call, wherever in
   *  the transcript it turns out to be, and is not shown at all if the
   *  transcript does not hold it. */
  messageIndex: number;
  approval: ApiApproval;
};

const byThread = new Map<string, readonly InlineApproval[]>();
const listeners = new Map<string, Set<() => void>>();

/** Shared so a thread with no approvals returns the same empty array every
 *  time. `useSyncExternalStore` compares snapshots by identity and would spin
 *  forever on a fresh `[]`. */
const NONE: readonly InlineApproval[] = [];

/** Record a snapshot against the turn that raised it, pending or resolved.
 *
 *  Snapshots are whole, so a request that has since been decided simply
 *  replaces its earlier self in place. */
export function publishApproval(
  threadId: string,
  approval: ApiApproval | null,
  messageIndex: number,
): void {
  if (!approval) return;
  const existing = byThread.get(threadId) ?? NONE;
  const at = existing.findIndex((entry) => entry.approval.id === approval.id);
  // A reconnecting browser is sent the current snapshot from scratch, so the
  // same one arrives again unchanged. Publishing it would be a re-render that
  // says nothing.
  if (at >= 0 && JSON.stringify(existing[at].approval) === JSON.stringify(approval))
    return;

  byThread.set(
    threadId,
    at >= 0
      ? existing.map((entry, index) =>
          index === at ? { messageIndex: entry.messageIndex, approval } : entry,
        )
      : [...existing, { messageIndex, approval }],
  );
  for (const listener of listeners.get(threadId) ?? []) listener();
}

export function readApprovals(threadId: string): readonly InlineApproval[] {
  return byThread.get(threadId) ?? NONE;
}

/** How a conversation answers what it was asked.
 *
 *  Beside the requests themselves rather than passed down to the card, because
 *  the two are the same fact: a conversation that publishes an approval is the
 *  conversation that has to be able to settle it, and every component between
 *  the transcript and the button would otherwise be carrying a pair of
 *  callbacks it has nothing to do with.
 *
 *  A chat is answered through its thread, which is what the default does. A
 *  graph WorkOrder's conversation belongs to the graph engine and is answered
 *  there, so it registers its own for as long as it is on screen. */
export type ApprovalActions = {
  decide(approvalId: string, decision: ApprovalDecision): Promise<unknown>;
  answer(approvalId: string, answers: Record<string, string[]>): Promise<unknown>;
};

const answering = new Map<string, ApprovalActions>();

/** Answer this conversation's requests with `actions` until the returned
 *  function is called. Only the registration still in place wins, so a page
 *  unmounting cannot take a later one's answering with it. */
export function answerApprovalsWith(
  threadId: string,
  actions: ApprovalActions,
): () => void {
  answering.set(threadId, actions);
  return () => {
    if (answering.get(threadId) === actions) answering.delete(threadId);
  };
}

export function approvalActions(threadId: string): ApprovalActions {
  return (
    answering.get(threadId) ?? {
      decide: (approvalId, decision) =>
        decideApproval(threadId, approvalId, decision),
      answer: (approvalId, answers) =>
        answerQuestion(threadId, approvalId, answers),
    }
  );
}

function subscribeApprovals(threadId: string, listener: () => void): () => void {
  const existing = listeners.get(threadId) ?? new Set<() => void>();
  existing.add(listener);
  listeners.set(threadId, existing);
  return () => {
    existing.delete(listener);
    if (!existing.size) listeners.delete(threadId);
  };
}

/** Everything this conversation has been asked to allow, oldest first.
 *
 *  The whole list rather than one turn's worth: filtering here would mint a new
 *  array on every render, and identity is how `useSyncExternalStore` tells a
 *  new snapshot from the same one. Callers narrow it with `useMemo`.
 */
export function useApprovals(
  threadId: string | null | undefined,
): readonly InlineApproval[] {
  const subscribe = useCallback(
    (listener: () => void) =>
      threadId ? subscribeApprovals(threadId, listener) : () => {},
    [threadId],
  );
  const snapshot = useCallback(
    () => (threadId ? readApprovals(threadId) : NONE),
    [threadId],
  );
  return useSyncExternalStore(subscribe, snapshot);
}
