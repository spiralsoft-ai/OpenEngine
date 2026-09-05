/** A graph node's conversation, read and steered through the chat's own view.
 *
 *  Two halves, and the second is the one worth having: `graphConversation` is a
 *  fold over events and can be checked directly, but what the page promises is
 *  that the folded turns reach assistant-ui and come back out as the same
 *  transcript a chat draws -- folded tool rows, approval cards under the
 *  command they are about, a composer that steers. That only holds if the
 *  runtime is wired the way this file renders it, so these render it.
 */

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiGraphEvent, ApiGraphRun } from "./api";
import {
  GraphConversationPage,
  graphConversation,
  graphConversationId,
} from "./graph-conversation";

const NODE = "implementation";

/** Published approvals are global and never forgotten, which is the point of
 *  them: a browser holds one page's worth for as long as the page is open. A
 *  run of its own per test is how one case stops being the next case's
 *  history. */
let runs = 0;
let runId = "run-1";
beforeEach(() => {
  runs += 1;
  runId = `run-${runs}`;
});

function json(body: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function event(overrides: Partial<ApiGraphEvent> & { type: string }): ApiGraphEvent {
  return { sequence: 1, nodeId: NODE, payload: {}, ...overrides };
}

function graphRun(overrides: Partial<ApiGraphRun> = {}): ApiGraphRun {
  return {
    runId,
    graphId: "implementation-review-codex",
    status: "running",
    activeExecutions: [{ executionId: "execution-1", nodeId: NODE }],
    nextNodes: [],
    values: {},
    pendingApprovals: [],
    error: "",
    ...overrides,
  };
}

/** A server that answers the two reads the page makes, and records writes. */
function serve(events: ApiGraphEvent[], run: ApiGraphRun = graphRun()) {
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === `/api/runs/${runId}/graph-events`) return json({ events });
    if (path === `/graph/api/runs/${runId}`) return json(run);
    if (path.startsWith(`/graph/api/runs/${runId}/`)) return json(run);
    return json({ error: `no route for ${path}` }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function open(events: ApiGraphEvent[], run?: ApiGraphRun) {
  serve(events, run);
  render(<GraphConversationPage runId={runId} nodeId={NODE} />);
  await act(async () => {});
  return screen;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("graphConversation", () => {
  it("opens with what the node was asked, then what it said back", () => {
    const { messages } = graphConversation([
      event({ sequence: 1, type: "conversation.started", payload: { agent: "codex" } }),
      event({
        sequence: 2,
        type: "transcript",
        payload: { role: "user", text: "Implement the change." },
      }),
      event({
        sequence: 3,
        type: "transcript",
        payload: { role: "assistant", text: "Reading the code." },
      }),
    ]);

    expect(messages).toEqual([
      { id: "user-2", role: "user", content: [{ type: "text", text: "Implement the change." }] },
      {
        id: "assistant-3",
        role: "assistant",
        content: [{ type: "text", text: "Reading the code." }],
      },
    ]);
  });

  it("keeps a call's result on the call, in the turn that made it", () => {
    const { messages } = graphConversation([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "assistant", text: "I'll read the tests." },
      }),
      event({
        sequence: 2,
        type: "tool.call",
        payload: { callId: "call-1", name: "Read runs.tsx", arguments: { kind: "read" } },
      }),
      event({
        sequence: 3,
        type: "tool.result",
        payload: { callId: "call-1", result: "completed" },
      }),
    ]);

    expect(messages).toEqual([
      {
        id: "assistant-1",
        role: "assistant",
        content: [
          { type: "text", text: "I'll read the tests." },
          {
            type: "tool-call",
            toolCallId: "call-1",
            toolName: "Read runs.tsx",
            args: { kind: "read" },
            result: "completed",
          },
        ],
      },
    ]);
  });

  it("stands up the call a request is about before the agent streams it", () => {
    // An agent asks permission first and reports the call afterwards, so
    // without this the question would have nothing to sit beside for exactly
    // as long as the run is waiting on a person.
    const asked = graphConversation(
      [
        event({
          sequence: 1,
          type: "approval.requested",
          payload: {
            approvalId: "approval-1",
            kind: "command_execution",
            reason: "run the tests",
            command: "pytest",
            toolName: "execute",
            toolCallId: "call-1",
          },
        }),
      ],
      [
        {
          approvalId: "approval-1",
          nodeId: NODE,
          reason: "run the tests",
          allowedDecisions: ["accept", "cancel"],
        },
      ],
    );

    expect(asked.messages[0].content).toEqual([
      {
        type: "tool-call",
        toolCallId: "call-1",
        toolName: "execute",
        args: { command: "pytest", reason: "run the tests" },
      },
    ]);
    expect(asked.requests[0].approval).toMatchObject({
      id: "approval-1",
      status: "pending",
      toolCallId: "call-1",
      command: "pytest",
      allowedDecisions: ["accept", "cancel"],
    });

    // And the call the agent then streams is that same call, described by the
    // agent rather than by its request, not a second one below it.
    const ran = graphConversation([
      event({
        sequence: 1,
        type: "approval.requested",
        payload: { approvalId: "approval-1", command: "pytest", toolCallId: "call-1" },
      }),
      event({
        sequence: 2,
        type: "approval.resolved",
        payload: { approvalId: "approval-1", decision: "accept" },
      }),
      event({
        sequence: 3,
        type: "tool.call",
        payload: { callId: "call-1", name: "pytest", arguments: { kind: "execute" } },
      }),
    ]);

    expect(ran.messages[0].content).toHaveLength(1);
    expect(ran.requests[0].approval).toMatchObject({
      status: "decided",
      decision: "accept",
    });
  });

  it("says a request nobody can answer any more is no longer open", () => {
    const { requests } = graphConversation([
      event({
        sequence: 1,
        type: "approval.requested",
        payload: { approvalId: "approval-1", command: "pytest" },
      }),
    ]);

    expect(requests[0].approval.status).toBe("interrupted");
  });

  it("shows a steering message once, where it was sent", () => {
    // Published when it is sent and again when the node picks it up. A reader
    // shown both would be reading somebody repeating themselves.
    const { messages } = graphConversation([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "assistant", text: "Done." },
      }),
      event({
        sequence: 2,
        type: "steering.received",
        payload: { message: "Use the fast suite." },
      }),
      event({
        sequence: 3,
        type: "transcript",
        payload: { role: "user", text: "Use the fast suite." },
      }),
      event({
        sequence: 4,
        type: "transcript",
        payload: { role: "assistant", text: "Ran the fast suite." },
      }),
    ]);

    expect(messages.map((message) => [message.role, message.content])).toEqual([
      ["assistant", [{ type: "text", text: "Done." }]],
      ["user", [{ type: "text", text: "Use the fast suite." }]],
      ["assistant", [{ type: "text", text: "Ran the fast suite." }]],
    ]);
  });
});

describe("GraphConversationPage", () => {
  it("draws the node's work as the conversation it is", async () => {
    await open([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "user", text: "Implement the change." },
      }),
      event({
        sequence: 2,
        type: "transcript",
        payload: { role: "assistant", text: "Reading the code." },
      }),
      event({
        sequence: 3,
        type: "tool.call",
        payload: { callId: "call-1", name: "Read runs.tsx", arguments: {} },
      }),
      event({
        sequence: 4,
        type: "tool.result",
        payload: { callId: "call-1", result: "completed" },
      }),
    ]);

    expect(screen.getByText("Implement the change.")).toBeVisible();
    expect(screen.getByText("Reading the code.")).toBeVisible();
    // The chat's own folded row, not a second rendering of one.
    expect(screen.getByText(/Read runs.tsx/)).toBeVisible();
    expect(document.querySelectorAll(".message-user")).toHaveLength(1);
    expect(document.querySelectorAll(".message-assistant")).toHaveLength(1);
  });

  it("puts a pending request under the command it is about, and answers it", async () => {
    const run = graphRun({
      status: "awaiting_approval",
      pendingApprovals: [
        {
          approvalId: "approval-1",
          nodeId: NODE,
          reason: "run the tests",
          allowedDecisions: ["accept", "cancel"],
        },
      ],
    });
    const fetch = serve(
      [
        event({
          sequence: 1,
          type: "transcript",
          payload: { role: "assistant", text: "I'll run the tests." },
        }),
        event({
          sequence: 2,
          type: "approval.requested",
          payload: {
            approvalId: "approval-1",
            kind: "command_execution",
            reason: "run the tests",
            command: "pytest",
            toolName: "execute",
            toolCallId: "call-1",
          },
        }),
      ],
      run,
    );
    render(<GraphConversationPage runId={runId} nodeId={NODE} />);
    await act(async () => {});

    const card = await screen.findByText("Approval needed · pytest");
    // Inside the call it is about: the run is stopped on this command, and a
    // card anywhere else would be about a command nobody asked about.
    expect(card.closest(".approvals-call")).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/graph/api/runs/${runId}/approvals/approval-1`,
        expect.objectContaining({ body: '{"decision":"accept"}' }),
      ),
    );
  });

  it("steers the agent that is working, and says so when there is none", async () => {
    const fetch = serve([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "assistant", text: "Reading the code." },
      }),
    ]);
    render(<GraphConversationPage runId={runId} nodeId={NODE} />);
    await act(async () => {});

    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "Rename the flag first.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/graph/api/runs/${runId}/steering`,
        expect.objectContaining({
          body: JSON.stringify({
            message: "Rename the flag first.",
            node: NODE,
          }),
        }),
      ),
    );
  });

  it("offers no composer to a node with nothing in flight", async () => {
    await open(
      [
        event({
          sequence: 1,
          type: "transcript",
          payload: { role: "assistant", text: "Done." },
        }),
      ],
      graphRun({ status: "completed", activeExecutions: [] }),
    );

    expect(screen.queryByLabelText("Message the agent")).toBeNull();
    expect(screen.getByText(/Nothing is running here/)).toBeVisible();
  });

  it("still reads a transcript the engine has no record of the run for", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === `/api/runs/${runId}/graph-events`)
          return json({
            events: [
              event({
                sequence: 1,
                type: "transcript",
                payload: { role: "assistant", text: "Wrote the greeting." },
              }),
            ],
          });
        return json({ error: "run not found" }, { status: 404 });
      }),
    );

    render(<GraphConversationPage runId={runId} nodeId={NODE} />);
    await act(async () => {});

    expect(screen.getByText("Wrote the greeting.")).toBeVisible();
    expect(screen.queryByLabelText("Message the agent")).toBeNull();
  });

  it("leads back to the WorkOrder the node belongs to", async () => {
    await open([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "assistant", text: "Done." },
      }),
    ]);

    expect(
      screen.getByRole("link", { name: new RegExp(`Back to WorkOrder ${runId}`) }),
    ).toHaveAttribute("href", `/runs/${runId}`);
  });

  it("names a conversation by its run and its node", () => {
    expect(graphConversationId("run-1", NODE)).toBe("graph--run-1--implementation");
  });

  it("keeps an open conversation current", async () => {
    // Real timers, because what the reader is waiting on is the same wait: the
    // page polls, and assistant-ui reveals a running turn's words as they
    // arrive rather than in one jump.
    let reads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === `/graph/api/runs/${runId}`) return json(graphRun());
        reads += 1;
        return json({
          events:
            reads === 1
              ? []
              : [
                  event({
                    sequence: 1,
                    type: "transcript",
                    payload: { role: "assistant", text: "Reading the code." },
                  }),
                ],
        });
      }),
    );

    render(<GraphConversationPage runId={runId} nodeId={NODE} />);
    await act(async () => {});
    expect(screen.queryByText("Reading the code.")).toBeNull();

    await waitFor(() => expect(screen.getByText("Reading the code.")).toBeVisible(), {
      timeout: 5000,
    });
  });

  it("counts what is on screen the way a chat does", async () => {
    await open([
      event({
        sequence: 1,
        type: "transcript",
        payload: { role: "assistant", text: "Reading the code." },
      }),
      event({
        sequence: 2,
        type: "tool.call",
        payload: { callId: "call-1", name: "Read runs.tsx", arguments: {} },
      }),
    ]);

    const stats = document.querySelector(".stats");
    expect(stats).not.toBeNull();
    expect(within(stats as HTMLElement).getByText("Tool calls").parentElement)
      .toHaveTextContent("1");
  });
});
