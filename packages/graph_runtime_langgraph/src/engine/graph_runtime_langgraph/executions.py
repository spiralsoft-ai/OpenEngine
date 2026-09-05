"""What a LangGraph task is while it runs, and how anything reaches it.

LangGraph gives every task it schedules an id that is stable for that
invocation, visible to the task itself as `__pregel_task_id` and to a stream
consumer as the `id` on a `tasks` update. That id is this package's
`ExecutionId`, which is the whole reason the mapping works: two `Send`s into one
node are two tasks with two ids, so they are two executions with two transcripts
and two sets of open questions, addressable apart from each other and from the
node name they share.

A `NodeExecution` is what that id resolves to while the task is in flight. It is
registered before the task's coroutine is created -- the runtime reads the
frontier off the checkpoint LangGraph publishes *before* it schedules the
superstep -- so there is no window in which something is running and cannot be
steered.

Nothing here suspends the graph. A steering message is put on this object's
queue and picked up by the node at its next interruption point; an approval is a
future this object holds and something outside resolves. The node's coroutine
stays exactly where it was through both, which is what keeps an agent session
alive across a question about the command it wants to run. Modelling either as a
LangGraph interrupt would end the task, discard the session, and re-enter the
node afterwards with the conversation gone.

`current_execution()` is how a node finds its own. It is two lookups rather than
one: a `ContextVar` says which run is being driven -- set once by the driver, and
inherited by every task LangGraph creates from it -- and LangGraph's own config
says which task this is. The context variable cannot carry the execution itself,
because all of a superstep's tasks inherit one context and would then share one
answer; the task id is the part that differs, and only LangGraph knows it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from langgraph.config import get_config

from engine.graph_runtime.control import ApprovalNotPendingError
from engine.graph_runtime.events import EventKind
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime.topology import NodeId

if TYPE_CHECKING:  # pragma: no cover - import cycle, and only for annotations
    from engine.graph_runtime_langgraph.runtime import LangGraphRuntime


class NoExecutionError(RuntimeError):
    """A node asked for its execution while nothing was driving it.

    Almost always a graph invoked directly -- `graph.ainvoke(...)` -- rather than
    through the runtime. Worth an error rather than a `None` that every node
    would have to branch on: a node that publishes transcript events and raises
    approvals has nowhere to send either.
    """


@dataclass(frozen=True, slots=True)
class DrivenRun:
    """Which run the surrounding task belongs to, and who is driving it."""

    runtime: "LangGraphRuntime"
    run_id: RunId


_CURRENT: ContextVar[DrivenRun | None] = ContextVar(
    "engine_graph_runtime_langgraph_run", default=None
)


@contextmanager
def driving(runtime: "LangGraphRuntime", run_id: RunId) -> Iterator[None]:
    """Mark everything started inside the block as part of `run_id`."""
    token = _CURRENT.set(DrivenRun(runtime, run_id))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_execution() -> "NodeExecution":
    """This task's execution. Raises `NoExecutionError` outside a driven run."""
    driven = _CURRENT.get()
    if driven is None:
        raise NoExecutionError(
            "this node is not running under a LangGraphRuntime, so it has no "
            "execution to publish events to or raise approvals against"
        )
    configurable = get_config().get("configurable", {})
    task_id = configurable.get("__pregel_task_id")
    if not isinstance(task_id, str):
        raise NoExecutionError(
            "LangGraph did not name the task running this node, so there is no "
            "stable id to address control to"
        )
    return driven.runtime.execution(driven.run_id, ExecutionId(task_id))


class NodeExecution:
    """One in-flight LangGraph task, as external control sees it.

    Satisfies `engine.graph_runtime.ControllableExecution`: two methods, neither
    of which knows what the node is running. An ACP-backed node attaches its
    live session with `attach`, and the answer to an approval reaches that
    session by resolving the future the session's permission handler is waiting
    on -- so `decide()` -> `ApprovalId` -> `ExecutionId` -> the session, with
    nothing ACP-shaped in the path above this object.
    """

    def __init__(
        self,
        runtime: "LangGraphRuntime",
        run_id: RunId,
        execution_id: ExecutionId,
        node_id: NodeId,
    ) -> None:
        self._runtime = runtime
        self.run_id = run_id
        self.execution_id = execution_id
        self.node_id = node_id
        self._steering: asyncio.Queue[str] = asyncio.Queue()
        self._waiting: dict[ApprovalId, asyncio.Future[ApprovalDecision]] = {}
        self._session: Any | None = None

    # --- what external control routes to us --------------------------------

    async def steer(self, message: str) -> None:
        """Queue an instruction for the node to take when it next looks.

        Queued rather than delivered, and that is the contract: an execution
        blocked on an approval must still be able to accept a message, and one
        that waited for the node to be ready could not.
        """
        self._steering.put_nowait(message)

    async def decide(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        waiting = self._waiting.get(approval_id)
        if waiting is None or waiting.done():
            raise ApprovalNotPendingError(
                f"approval is no longer pending: {approval_id}"
            )
        waiting.set_result(decision)

    # --- what the node running inside us uses ------------------------------

    @property
    def runtime(self) -> "LangGraphRuntime":
        """The runtime driving this execution, for a node that needs its store."""
        return self._runtime

    @property
    def session(self) -> Any | None:
        """The `ACPSession` this execution is driving, once it has one.

        `None` for a node that is not an agent. Exposed rather than private
        because "which conversation is this execution?" is a question a node's
        own code answers, and hiding it would make every ACP node keep a second
        copy of the same object.
        """
        return self._session

    def attach(self, session: Any) -> None:
        """Adopt the ACP session this execution is now driving."""
        self._session = session

    async def emit(
        self,
        kind: EventKind,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        """Publish something this execution did, stamped with who did it."""
        await self._runtime.publish(
            self.run_id, kind, payload, self.node_id, self.execution_id
        )

    async def say(self, text: str, role: str = "assistant") -> None:
        await self.emit(EventKind.TRANSCRIPT, {"role": role, "text": text})

    async def tool(
        self,
        call_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        result: str = "",
    ) -> None:
        await self.emit(
            EventKind.TOOL_CALL,
            {"callId": call_id, "name": name, "arguments": dict(arguments or {})},
        )
        await self.emit(
            EventKind.TOOL_RESULT, {"callId": call_id, "name": name, "result": result}
        )

    async def next_message(self) -> str:
        """Wait for an instruction. The interruption point steering arrives at."""
        return await self._steering.get()

    def pending_messages(self) -> tuple[str, ...]:
        """Everything queued right now, taken without waiting for more."""
        taken: list[str] = []
        while not self._steering.empty():
            taken.append(self._steering.get_nowait())
        return tuple(taken)

    async def ask(
        self,
        *,
        reason: str,
        kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION,
        command: str = "",
        tool_name: str = "",
        session_key: str = "",
        continuation: Any | None = None,
        request: Mapping[str, object] | None = None,
        approval_id: ApprovalId | None = None,
        tool_call_id: str = "",
    ) -> ApprovalDecision:
        """Raise a request for consent and wait, without leaving the node.

        The request is written to the runtime's store before this returns to
        waiting, so a process that dies here leaves an answerable question
        behind rather than a lost one. `continuation` is what makes answering it
        later mean something: it names the conversation to reconnect to.

        `tool_call_id` is which of the turn's calls the question is about, for
        a client drawing the question beside it. Empty when it is about none.
        """
        return await self._runtime.raise_approval(
            self,
            reason=reason,
            kind=kind,
            command=command,
            tool_name=tool_name,
            session_key=session_key,
            continuation=continuation,
            request=request,
            approval_id=approval_id,
            tool_call_id=tool_call_id,
        )

    # --- the runtime's own bookkeeping -------------------------------------

    def expect(self, approval_id: ApprovalId) -> asyncio.Future[ApprovalDecision]:
        """Open a slot for an answer to `approval_id` and hand back the wait."""
        waiting: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[approval_id] = waiting
        return waiting

    def forget(self, approval_id: ApprovalId) -> None:
        self._waiting.pop(approval_id, None)

    def abandon(self) -> None:
        """Give up every open question. What stopping this execution means.

        The futures are cancelled rather than left: whatever was awaiting one is
        about to be cancelled too, and a future nobody retrieves is a warning on
        the way out of an otherwise clean shutdown.
        """
        for waiting in tuple(self._waiting.values()):
            if not waiting.done():
                waiting.cancel()
        self._waiting.clear()


__all__ = [
    "DrivenRun",
    "NoExecutionError",
    "NodeExecution",
    "current_execution",
    "driving",
]
