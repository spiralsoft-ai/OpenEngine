/** Where in a turn an approval is shown.
 *
 *  The one thing the cards cannot say for themselves: a request rendered
 *  perfectly in the wrong place tells the reader the agent asked about a
 *  command it never asked about. So this reads the rendered order rather than
 *  the filtering behind it, and assistant-ui's message primitives are replaced
 *  by the smallest things that hand one turn's parts to our renderer.
 */

import { render } from "@testing-library/react";
import type { ComponentProps, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiApproval } from "./api";
import { publishApproval } from "./approvals";
import { AssistantMessage } from "./chat";

type Part =
  | { type: "text"; text: string }
  | {
      type: "tool-call";
      toolCallId: string;
      toolName: string;
      args: Record<string, unknown>;
      argsText: string;
      status: { type: string };
    };

const turn = vi.hoisted(() => ({
  parts: [] as unknown[],
  message: { index: 0, isLast: true, status: { type: "complete" } },
  threadListItem: { remoteId: "thread-placement" },
  thread: { messages: [] as { content: unknown[] }[] },
}));

vi.mock("@assistant-ui/react", async () => {
  const { createContext, createElement, useContext } = await import("react");
  const PartContext = createContext<Part | null>(null);
  return {
    useAuiState: (select: (state: typeof turn) => unknown) => select(turn),
    useToolCallElapsed: () => undefined,
    MessagePrimitive: {
      Root: ({ children, ...props }: ComponentProps<"div">) => (
        <div {...props}>{children}</div>
      ),
      Error: () => null,
      Parts: ({ children }: { children: (props: { part: unknown }) => ReactNode }) => (
        <>
          {turn.parts.map((part, index) => (
            <PartContext.Provider key={index} value={part as Part}>
              {children({ part })}
            </PartContext.Provider>
          ))}
        </>
      ),
    },
    MessagePartPrimitive: {
      Text: ({ component }: { component: string }) => {
        const part = useContext(PartContext);
        return createElement(component, null, part?.type === "text" ? part.text : "");
      },
    },
  };
});

function approval(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return {
    id: "approval-1",
    status: "decided",
    kind: "command_execution",
    reason: null,
    command: "rm -rf build",
    cwd: "/repo",
    toolName: "Bash",
    toolCallId: null,
    arguments: null,
    allowedDecisions: ["accept", "cancel"],
    decision: "accept",
    decisionSource: "user",
    ...overrides,
  };
}

/** A pause over something that is not a tool call, so it names none: the only
 *  kind of request with nowhere of its own in the transcript to sit. */
function unplacedQuestion(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return approval({
    id: "approval-3",
    kind: "user_input",
    status: "pending",
    command: null,
    toolName: null,
    toolCallId: null,
    decision: null,
    decisionSource: null,
    // The questions are what make it one: a request under this kind that
    // carries none is a verdict rather than a form, and reads as one.
    questions: [
      {
        id: "api",
        header: "API",
        question: "Question",
        options: [{ label: "Public", description: "Preserve the public API" }],
        multiSelect: false,
        allowsOther: false,
      },
    ],
    ...overrides,
  });
}

function call(toolCallId: string, toolName: string, detail: string): Part {
  return {
    type: "tool-call",
    toolCallId,
    toolName,
    args: { command: detail },
    argsText: JSON.stringify({ command: detail }),
    status: { type: "complete" },
  };
}

/** One turn as the reader meets it: every folded row and paragraph, in order. */
function rendered(): string[] {
  return Array.from(
    document.querySelectorAll(".tool-title, .approval-summary, .message-assistant > p"),
  ).map((node) => node.textContent ?? "");
}

/** Snapshots are global and never forgotten, which is the point of them. A
 *  thread of its own per test is how one case stops being the next case's
 *  history. */
let threads = 0;
beforeEach(() => {
  threads += 1;
  turn.threadListItem.remoteId = `thread-placement-${threads}`;
  turn.parts = [];
  turn.thread.messages = [];
});

describe("where a turn shows what it was asked to allow", () => {
  it("puts each request under its own call, and the answer below both", () => {
    const parts = [
      call("call-1", "Bash", "rm -rf build"),
      call("call-2", "Bash", "ls"),
      call("call-3", "Edit", "src/api.ts"),
      { type: "text" as const, text: "Cleaned the tree and fixed the export." },
    ];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    publishApproval(threadId, approval({ toolCallId: "call-1" }), 0);
    publishApproval(
      threadId,
      approval({ id: "approval-2", toolCallId: "call-3", command: null, toolName: "Edit" }),
      0,
    );

    render(<AssistantMessage />);

    expect(rendered()).toEqual([
      "ran Bash",
      "Approved · rm -rf build",
      "ran Bash",
      "ran Edit",
      "Approved · Edit",
      "Cleaned the tree and fixed the export.",
    ]);
  });

  it("shows nothing for a request naming a call this transcript does not hold", () => {
    const parts = [call("call-1", "Bash", "ls"), { type: "text" as const, text: "Done." }];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    // It named a call, so beside that call is the only place it means anything.
    // The end of the turn is not somewhere it may claim instead.
    publishApproval(
      threadId,
      approval({ id: "approval-4", toolCallId: "call-gone", command: "git push" }),
      0,
    );

    render(<AssistantMessage />);

    expect(rendered()).toEqual(["ran Bash", "Done."]);
  });

  it("ends the turn with a pending request that names no call, which has nowhere else", () => {
    const parts = [call("call-1", "Bash", "ls"), { type: "text" as const, text: "Done." }];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    // A pause over something that is not a tool call. Unrendered, it is a run
    // waiting on an answer nobody can see to give.
    publishApproval(threadId, unplacedQuestion(), 0);

    render(<AssistantMessage />);

    expect(rendered()).toEqual(["ran Bash", "Done.", "Answer needed · Question"]);
  });

  it("takes that card away once the request has been answered", () => {
    const parts = [call("call-1", "Bash", "ls"), { type: "text" as const, text: "Done." }];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    publishApproval(threadId, unplacedQuestion(), 0);
    // Answered, it is a result with no call to sit beside, and a result stacked
    // at the end of the page reads as a comment on whatever the turn did last.
    publishApproval(
      threadId,
      unplacedQuestion({ status: "decided", decision: "accept", decisionSource: "user" }),
      0,
    );

    render(<AssistantMessage />);

    expect(rendered()).toEqual(["ran Bash", "Done."]);
  });
});
