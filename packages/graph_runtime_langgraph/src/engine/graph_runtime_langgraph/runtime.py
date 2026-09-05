"""`GraphRuntime`, implemented by letting LangGraph be the graph.

Almost nothing here decides anything. The contract's four primitives already
exist in Pregel, and the work is translation rather than reimplementation:

    RunId               a LangGraph thread
    CheckpointId        a LangGraph checkpoint, addressed by its own id
    ExecutionId         the task id LangGraph gives one invocation of one node
    next_nodes          the `next` of the checkpoint the thread is standing at

The two places that are *not* translation are worth naming, because both would
be easy to get subtly wrong:

**Starting.** A run is seeded with `aupdate_state` before anything executes,
rather than by handing the input to `astream`. That is what lets `start()`
answer with a real position -- a checkpoint id a resume can name, and the
frontier it would replay -- instead of a snapshot of a graph that has already
begun running. It also removes LangGraph's `__start__` bookkeeping checkpoint
from the history a client reads, which would otherwise be a second position with
nothing at it.

**Forking.** `resume_from` writes the fork checkpoint itself, as a child of the
one being re-attempted, and only then starts executing from it. LangGraph will
happily replay from an earlier checkpoint on its own, but it decides for itself
whether that leaves a new position behind -- and the contract promises a
`CheckpointId` the caller can already see in `history()`, hanging off the
attempt it replaces. So the copy is made deliberately, with `create_checkpoint`
and the saver both graphs are compiled against; nothing is removed, and the
abandoned attempt keeps its own children.

Everything the runtime knows about a run's *position* it asks LangGraph for
every time. Nothing here caches a frontier or a set of values, because a second
copy of the graph's state is a second answer to "where is this run?" and it will
eventually be the wrong one.

What LangGraph does not persist is in `engine.graph_runtime_langgraph.store`:
which graph a thread is of, and what an execution stopped to ask. Both are
needed by a process that did not start the run, which is the whole point of the
approval handoff -- see `engine.graph_runtime_langgraph.acp`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from langgraph.checkpoint.base import create_checkpoint

from engine.graph_runtime.checkpoints import Checkpoint, CheckpointId
from engine.graph_runtime.control import (
    CANCELLED,
    ApprovalNotPendingError,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    RunStatus,
    UnknownApprovalError,
    UnknownCheckpointError,
    UnknownGraphError,
    UnknownRunError,
)
from engine.graph_runtime.events import EventKind, EventObserver, RuntimeEvent
from engine.graph_runtime.executions import ExecutionRegistry
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime.topology import GraphId, GraphTopology, NodeId
from engine.graph_runtime_langgraph.executions import NodeExecution, driving
from engine.graph_runtime_langgraph.graphs import START, LangGraphDefinition
from engine.graph_runtime_langgraph.store import (
    ApprovalRecord,
    GraphRuntimeStore,
    InMemoryGraphRuntimeStore,
    RunRecord,
)

#: Where a failure that a client only sees as a sentence is written down whole.
#: A run's `error` is one string, published once and stored once, with no type
#: and no traceback; this is where the operator reading the process log finds
#: what actually raised. Written by `_fail`, which is what decides whether this
#: failure is the one being reported.
log = logging.getLogger(__name__)

#: LangGraph's reasons a checkpoint exists, in the contract's vocabulary.
#: `update` is here because a run is seeded with one -- that seeded position is
#: what "start" means for a thread this runtime opened.
_SOURCES: Mapping[str, str] = {
    "input": "start",
    "update": "start",
    "loop": "superstep",
    "fork": "fork",
}


@dataclass(slots=True)
class _Live:
    """What a run has in *this* process. Everything else is in the store."""

    run_id: RunId
    graph_id: GraphId
    task: asyncio.Task[None] | None = None
    executions: dict[ExecutionId, NodeExecution] | None = None
    published: set[CheckpointId] | None = None
    """Checkpoints already announced, so a resume does not re-announce one."""
    control: asyncio.Lock | None = None
    """Serialises the operations that stop and restart this run.

    Held for the whole of `resume_from`, because stopping is asynchronous: the
    guard is a stretch of time rather than a check, and a second request
    arriving inside it would find nothing left to stop and start a driver of its
    own -- two of them then interleaving into one thread's checkpoints.
    """

    def __post_init__(self) -> None:
        self.executions = {} if self.executions is None else self.executions
        self.published = set() if self.published is None else self.published
        self.control = asyncio.Lock() if self.control is None else self.control


class LangGraphRuntime:
    """Drive compiled LangGraphs through the generic control surface."""

    def __init__(
        self,
        *graphs: LangGraphDefinition,
        store: GraphRuntimeStore | None = None,
    ) -> None:
        self._definitions = {graph.graph_id: graph for graph in graphs}
        self._store: GraphRuntimeStore = store or InMemoryGraphRuntimeStore()
        self._observer: EventObserver | None = None
        self._registry = ExecutionRegistry()
        self._live: dict[RunId, _Live] = {}
        self._entries: dict[NodeId, int] = {}

    # --- the contract ------------------------------------------------------

    def observe(self, observer: EventObserver) -> None:
        self._observer = observer

    def graphs(self) -> tuple[GraphTopology, ...]:
        return tuple(graph.topology for graph in self._definitions.values())

    def topology(self, graph_id: GraphId) -> GraphTopology | None:
        definition = self._definitions.get(graph_id)
        return definition.topology if definition is not None else None

    async def start(
        self, graph_id: GraphId, values: Mapping[str, object]
    ) -> RunSnapshot:
        definition = self._definitions.get(graph_id)
        if definition is None:
            raise UnknownGraphError(f"unknown graph: {graph_id}")
        run_id = RunId(f"run-{uuid4().hex[:12]}")
        await self._store.remember_run(RunRecord(run_id, graph_id))
        live = _Live(run_id, graph_id)
        self._live[run_id] = live
        await self.publish(run_id, EventKind.RUN_STARTED, {"values": dict(values)})
        # Seeded rather than streamed in: the position a run begins at is a
        # checkpoint somebody may want to send it back to, and it has to exist
        # before `start` answers or the id would be one the caller never saw.
        config = await definition.graph.aupdate_state(
            self._config(run_id), dict(values)
        )
        opening = await self._position(definition, config)
        await self._announce(live, opening)
        self._launch(live, definition, config)
        return await self._snapshot(run_id)

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        record = await self._store.run(run_id)
        return None if record is None else await self._snapshot(run_id)

    async def history(self, run_id: RunId) -> tuple[Checkpoint, ...]:
        definition = await self._definition_for(run_id)
        saved = [
            state
            async for state in definition.graph.aget_state_history(
                self._config(run_id)
            )
        ]
        # LangGraph answers newest first, and checkpoint ids sort by time, so
        # reversing is the oldest-first order the contract asks for -- with a
        # fork's descendants after the attempt they replace rather than
        # interleaved with it.
        return tuple(self._checkpoint(definition, state) for state in reversed(saved))

    async def resume_from(
        self, run_id: RunId, checkpoint_id: CheckpointId
    ) -> RunSnapshot:
        definition = await self._definition_for(run_id)
        at = self._config(run_id, checkpoint_id)
        saved = await definition.graph.checkpointer.aget_tuple(at)
        if saved is None:
            raise UnknownCheckpointError(f"unknown checkpoint: {checkpoint_id}")
        live = self._live.setdefault(
            run_id, _Live(run_id, (await self._require(run_id)).graph_id)
        )
        # Held across the stop as well as the fork. Two of these arriving at
        # once are serialised rather than dropped: both are honoured, and the
        # second one's first act is stopping the driver the first one started.
        async with live.control:
            await self._stop(live)
            # Questions raised by the attempt being replaced can never be
            # answered: the executions that asked them are gone. Recorded as
            # settled rather than deleted, so a client still showing one is told
            # it is no longer pending rather than that it never existed.
            await self._store.abandon_run_approvals(run_id)
            await self._store.remember_run(
                replace(await self._require(run_id), error="")
            )
            step = saved.metadata.get("step", 0)
            forked = await definition.graph.checkpointer.aput(
                saved.config,
                create_checkpoint(saved.checkpoint, None, step),
                {
                    "source": "fork",
                    "step": step,
                    "parents": saved.metadata.get("parents", {}),
                },
                {},
            )
            position = await self._position(definition, forked)
            live.published.add(position.checkpoint_id)
            await self.publish(
                run_id,
                EventKind.RUN_FORKED,
                {
                    "from": str(checkpoint_id),
                    "checkpointId": str(position.checkpoint_id),
                    "nodes": [str(node_id) for node_id in position.next_nodes],
                    "values": dict(position.values),
                },
            )
            # Read before the driver is started, so the caller is told about the
            # position it asked for rather than about whatever the restarted
            # superstep has already got to.
            answer = await self._snapshot(run_id)
            self._launch(live, definition, forked)
            return answer

    async def steer(
        self,
        run_id: RunId,
        message: str,
        execution_id: ExecutionId | None = None,
        node_id: NodeId | None = None,
    ) -> RunSnapshot:
        await self._require(run_id)
        target, execution = self._registry.resolve(run_id, execution_id, node_id)
        await execution.steer(message)
        # Accepted for delivery, not delivered: the execution takes it at its
        # next interruption point and says so itself. Blocking until then would
        # mean an agent waiting on an approval could never be redirected.
        await self.publish(
            run_id,
            EventKind.STEERING_RECEIVED,
            {"message": message},
            target.node_id,
            target.execution_id,
        )
        return await self._snapshot(run_id)

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        await self._require(run_id)
        record = await self._store.approval(approval_id)
        if record is None or record.run_id != run_id:
            raise UnknownApprovalError(f"unknown approval: {approval_id}")
        if not record.pending:
            raise ApprovalNotPendingError(
                f"approval is no longer pending: {approval_id}"
            )
        await self._store.resolve_approval(approval_id, decision)
        await self._deliver(record, decision)
        await self.publish(
            run_id,
            EventKind.APPROVAL_RESOLVED,
            {"approvalId": str(approval_id), "decision": decision.value},
            record.node_id,
            record.execution_id,
        )
        if decision is ApprovalDecision.CANCEL:
            await self._refuse(record)
        return await self._snapshot(run_id)

    async def cancel(self, run_id: RunId) -> RunSnapshot:
        record = await self._require(run_id)
        live = self._live.setdefault(run_id, _Live(run_id, record.graph_id))
        # The same lock a fork holds, for the same reason: stopping is
        # asynchronous, and a resume arriving inside it would start a driver for
        # a run that is being ended.
        async with live.control:
            # Read before anything is stopped, because stopping changes the
            # answer: releasing the executions settles the approvals a waiting
            # run is waiting on, and a run read afterwards would look like one
            # that was simply working.
            ending = (await self._snapshot(run_id)).status
            stopping = ending not in (RunStatus.COMPLETED, RunStatus.FAILED)
            if stopping:
                # Written down before anything is let go, in the order and for
                # the reason `_refuse` uses: the executions the stop releases
                # may raise on their way out, and the first reason recorded is
                # the one a client was already told.
                await self._store.remember_run(replace(record, error=CANCELLED))
            await self._stop(live)
            # Settled rather than deleted, and without an `approval.resolved`
            # for any of them: nobody decided these, and the execution that
            # asked is gone, so nobody can.
            await self._store.abandon_run_approvals(run_id)
            if stopping:
                # Published here rather than through `_fail`, which logs the
                # ending with a traceback for an operator to read. A run stopped
                # because a person threw its WorkOrder away is not a fault, and
                # there is no exception behind it to print.
                await self.publish(
                    live.run_id, EventKind.RUN_FAILED, {"error": CANCELLED}
                )
            return await self._snapshot(run_id)

    async def aclose(self) -> None:
        for live in tuple(self._live.values()):
            await self._stop(live)

    # --- what a test may ask that a client cannot --------------------------

    def running(self) -> tuple[RunId, ...]:
        """Runs whose driver is still alive. Shutdown has to leave this empty."""
        return tuple(
            live.run_id for live in self._live.values() if self.executors(live.run_id)
        )

    def executors(self, run_id: RunId) -> tuple[asyncio.Task[None], ...]:
        """The tasks driving this run, which must never be more than one.

        Not part of `GraphRuntime`, and the same blind spot the fake's version
        exists to cover: two drivers over one thread are invisible from outside,
        because everything either does is something one of them could plausibly
        have done alone -- until their checkpoints start interleaving.
        """
        live = self._live.get(run_id)
        if live is None or live.task is None or live.task.done():
            return ()
        return (live.task,)

    def entered(self, node_id: NodeId) -> int:
        """How many LangGraph tasks of this node have been started, ever.

        Counted off the tasks LangGraph actually scheduled, which is what makes
        "did steering restart the node?" answerable: a runtime that delivered an
        instruction by replaying the node would show two.
        """
        return self._entries.get(node_id, 0)

    # --- what a node running inside us uses --------------------------------

    def execution(self, run_id: RunId, execution_id: ExecutionId) -> NodeExecution:
        """The handle for one in-flight task, creating it if the driver has not.

        Ordinarily the driver has: it registers a superstep's executions from
        the checkpoint LangGraph publishes before scheduling any of them. The
        fallback matters for a task LangGraph schedules outside that -- a
        retried node, a `Send` accepted mid-superstep -- which must still be
        addressable rather than silently uncontrollable.
        """
        live = self._live.setdefault(run_id, _Live(run_id, GraphId("")))
        found = live.executions.get(execution_id)
        if found is not None:
            return found
        return self._acquire(live, execution_id, NodeId(""))

    async def publish(
        self,
        run_id: RunId,
        kind: EventKind,
        payload: Mapping[str, object] | None = None,
        node_id: NodeId | None = None,
        execution_id: ExecutionId | None = None,
    ) -> None:
        if self._observer is None:
            return
        await self._observer(
            RuntimeEvent(
                run_id=run_id,
                kind=kind,
                payload=dict(payload or {}),
                node_id=node_id,
                execution_id=execution_id,
            )
        )

    async def raise_approval(
        self,
        execution: NodeExecution,
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
        """Ask, write the question down, and wait without leaving the node.

        Written down *before* the wait, so a process that dies here leaves an
        answerable question rather than a lost one: everything a later process
        needs to route the answer -- which run, which execution, and the ACP
        continuation that reaches the conversation -- is in the store by the
        time this suspends.

        `tool_call_id` travels on the event because a question is about
        something: an agent asks to run *this* command, and a reader shown the
        question apart from the call it is about has to guess which of the
        turn's calls it means. It names the call in the agent's own vocabulary,
        which is the same id the `tool.call` event carries -- so a client can
        show the two together without matching on wording. Empty for a request
        that is about no call at all, such as a person's verdict on a run.
        """
        chosen = approval_id or ApprovalId(f"approval-{uuid4().hex[:12]}")
        record = ApprovalRecord(
            approval_id=chosen,
            run_id=execution.run_id,
            execution_id=execution.execution_id,
            node_id=execution.node_id,
            kind=kind,
            reason=reason,
            command=command,
            tool_name=tool_name,
            session_key=session_key,
            continuation=continuation,
            request=dict(request or {}),
        )
        await self._store.remember_approval(record)
        waiting = execution.expect(chosen)
        await self.publish(
            execution.run_id,
            EventKind.APPROVAL_REQUESTED,
            {
                "approvalId": str(chosen),
                "kind": kind.value,
                "reason": reason,
                "command": command,
                "toolName": tool_name,
                "toolCallId": tool_call_id,
            },
            execution.node_id,
            execution.execution_id,
        )
        try:
            return await waiting
        finally:
            execution.forget(chosen)

    async def recorded_decision(
        self, approval_id: ApprovalId
    ) -> ApprovalDecision | None:
        """An answer given while nothing was waiting for it.

        How a node re-entered after a restart learns what the person said. See
        `engine.graph_runtime_langgraph.acp`.
        """
        record = await self._store.approval(approval_id)
        return None if record is None else record.decision

    @property
    def store(self) -> GraphRuntimeStore:
        """Where a node writes what has to survive it."""
        return self._store

    # --- driving -----------------------------------------------------------

    def _launch(
        self, live: _Live, definition: LangGraphDefinition, config: Mapping[str, Any]
    ) -> None:
        live.task = asyncio.create_task(self._drive(live, definition, dict(config)))

    async def _drive(
        self, live: _Live, definition: LangGraphDefinition, config: dict[str, Any]
    ) -> None:
        """Consume the graph's stream, and make sure a failure is reported.

        Without the last part a node that raised would leave a run claiming to
        be running forever, holding every subscriber waiting for a terminal
        event and refusing steering as having nothing in flight.
        """
        failed_at: NodeId | None = None
        try:
            with driving(self, live.run_id):
                async with aclosing(
                    definition.graph.astream(
                        None, config, stream_mode=["checkpoints", "tasks"]
                    )
                ) as stream:
                    async for mode, chunk in stream:
                        if mode == "checkpoints":
                            await self._on_checkpoint(live, definition, chunk)
                        else:
                            failed_at = await self._on_task(live, chunk) or failed_at
            await self._release_all(live)
            await self.publish(
                live.run_id,
                EventKind.RUN_FINISHED,
                {"values": dict((await self._state(definition, live.run_id)).values)},
            )
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            await self._release_all(live)
            blamed = await self._blamed(definition, live.run_id) or failed_at
            await self._fail(live, str(failure), blamed, failure)

    async def _blamed(
        self, definition: LangGraphDefinition, run_id: RunId
    ) -> NodeId | None:
        """Which node raised, as the checkpoint that survived it records.

        Not from the stream: LangGraph publishes a `tasks` result for a task
        that returned and simply propagates the exception for one that did not,
        so the failure never arrives as an event. The uncommitted checkpoint
        does keep it, next to the frontier a resume would replay -- which is the
        same place a client is told to look.
        """
        state = await self._state(definition, run_id)
        return next(
            (NodeId(task.name) for task in state.tasks if task.error is not None),
            None,
        )

    async def _on_checkpoint(
        self, live: _Live, definition: LangGraphDefinition, chunk: Mapping[str, Any]
    ) -> None:
        """A superstep boundary: announce it, then adopt what it is about to run.

        Both halves happen here rather than off the `tasks` stream because
        LangGraph publishes this before it schedules any of the tasks, and
        `astream` only advances when this loop asks it to. So every execution is
        registered and announced before the node it belongs to can run a line --
        there is no window in which something is executing and cannot be
        steered, and no ordering to race over.
        """
        checkpoint = self._checkpoint_from_stream(definition, chunk)
        await self._release_all(live)
        if checkpoint.checkpoint_id not in live.published:
            await self._announce(live, checkpoint)
        for task in chunk.get("tasks", ()):
            node_id = NodeId(str(task.get("name")))
            execution_id = ExecutionId(str(task.get("id")))
            self._acquire(live, execution_id, node_id)
            self._entries[node_id] = self._entries.get(node_id, 0) + 1
            await self.publish(
                live.run_id, EventKind.NODE_STARTED, None, node_id, execution_id
            )

    async def _on_task(self, live: _Live, chunk: Mapping[str, Any]) -> NodeId | None:
        """One task started or ended. Returns the node that raised, if one did."""
        if "input" in chunk:
            # A task LangGraph scheduled after publishing the checkpoint -- a
            # `Send` accepted mid-superstep. Adopting it here keeps it
            # addressable; a task already registered is left alone.
            self._acquire(
                live,
                ExecutionId(str(chunk.get("id"))),
                NodeId(str(chunk.get("name"))),
            )
            return None
        node_id = NodeId(str(chunk.get("name")))
        execution_id = ExecutionId(str(chunk.get("id")))
        if chunk.get("error") is not None:
            return node_id
        await self.publish(
            live.run_id, EventKind.NODE_FINISHED, None, node_id, execution_id
        )
        return None

    async def _announce(self, live: _Live, checkpoint: Checkpoint) -> None:
        live.published.add(checkpoint.checkpoint_id)
        await self.publish(
            live.run_id,
            EventKind.CHECKPOINT,
            {
                "checkpointId": str(checkpoint.checkpoint_id),
                "parentId": (
                    str(checkpoint.parent_id) if checkpoint.parent_id else None
                ),
                "nextNodes": [str(node_id) for node_id in checkpoint.next_nodes],
                "source": checkpoint.source,
                "values": dict(checkpoint.values),
            },
        )

    async def _fail(
        self,
        live: _Live,
        error: str,
        node_id: NodeId | None,
        failure: BaseException,
    ) -> None:
        """Stop the run, once, and write down what stopped it.

        A refusal and the node noticing it are two things that can both arrive:
        the decision ends the run, and the agent it released may raise on its
        way out before the cancellation reaches it. The first answer is the one
        a client was already told, so a second would contradict it. A fork
        clears the error, which is what lets a re-attempt fail on its own.

        The log line is written here rather than where the exception was caught
        because it is subject to the same "once": the exception a refused run
        raises on its way out is the expected end of a run that ended for a
        reason a person chose, and logging it as a failure would put a
        traceback in the log every time somebody said no.
        """
        record = await self._store.run(live.run_id)
        if record is not None and record.error:
            return
        # Identifiers in the message, not in `extra`: no process here installs a
        # handler that renders extra fields, so a run id put there is a run id
        # nobody can read.
        log.error(
            "graph run %s (graph %s, node %s) failed: %s",
            live.run_id,
            live.graph_id,
            node_id if node_id is not None else "unknown",
            error,
            exc_info=failure,
        )
        if record is not None:
            await self._store.remember_run(replace(record, error=error))
        await self.publish(
            live.run_id, EventKind.RUN_FAILED, {"error": error}, node_id
        )

    async def _refuse(self, record: ApprovalRecord) -> None:
        """What "no" does to the run that asked.

        The request was execution-level -- may this agent run this command --
        but refusing it ends the work the node was doing, and a graph that
        carried on would be running an agent that had just been told to stop.
        The position is untouched: the superstep never committed, so the run can
        be sent back and tried again.

        It takes the siblings with it. A superstep is plural, so refusing one of
        three reviewers ends a run whose other two are mid-question -- and their
        questions are now unanswerable, because the superstep they were part of
        is over. Leaving them pending would report a finished run as still
        waiting on a person, and leave two executions accepting steering and
        decisions after the run had published its ending.
        """
        run = await self._require(record.run_id)
        live = self._live.setdefault(
            record.run_id, _Live(record.run_id, run.graph_id)
        )
        failure = f"{record.reason} was not allowed"
        # Written down before anything is let go, so that this is the reason the
        # run stopped. The agent released by the refusal may well raise on its
        # way out, and the first answer recorded is the one a client was already
        # told -- see `_fail`.
        await self._store.remember_run(replace(run, error=failure))
        # One turn of the loop, deliberately: resolving the approval made the
        # execution runnable, and the answer it owes its agent is written by a
        # task that is already scheduled. Stopping the run without letting
        # either run would leave the agent waiting on a reply nobody sent.
        await asyncio.sleep(0)
        await self._stop(live)
        # Settled rather than deleted, and without an `approval.resolved` for
        # any of them: nobody decided these, so a client showing one is told it
        # is no longer pending rather than that it was answered or never was.
        await self._store.abandon_run_approvals(record.run_id)
        await self.publish(
            record.run_id,
            EventKind.RUN_FAILED,
            {"error": failure},
            record.node_id,
        )

    async def _deliver(
        self, record: ApprovalRecord, decision: ApprovalDecision
    ) -> None:
        """Hand the answer to whoever is waiting, or restart what was.

        The fast path is the ordinary one: the execution that asked is still in
        flight, and resolving its future releases the agent session that has
        been holding the turn all along.

        The other path is the reason approvals are written down. Nothing is
        waiting -- the process that asked is gone -- so the run is started again
        from the checkpoint it stopped at. LangGraph re-enters the node, the
        node recognizes the continuation it left behind, and the same ACP
        conversation is resumed rather than a second one started. Which is what
        makes "the person answers on Monday" a thing that can work at all.

        And it waits for the last of them. A superstep is plural, so a lost
        process can leave three agents mid-question, and restarting re-enters
        all three -- not just the one whose answer arrived. A node whose
        question is still outstanding has no decision to apply, so it would
        open a second conversation and put its original prompt to it as fresh
        work, and the answer sent later would arrive for an execution that no
        longer exists. So a restart happens once, when nothing is left
        unanswered, and until then a decision is durable and inert.
        """
        try:
            _, execution = self._registry.resolve(record.run_id, record.execution_id)
        except RunNotSteerableError:
            if decision is ApprovalDecision.CANCEL:
                return  # `_refuse` ends the run; there is nothing to restart.
            if await self._store.pending_approvals(record.run_id):
                return
            await self._restart(record.run_id)
            return
        await execution.decide(record.approval_id, decision)

    async def _restart(self, run_id: RunId) -> None:
        definition = await self._definition_for(run_id)
        live = self._live.setdefault(
            run_id, _Live(run_id, (await self._require(run_id)).graph_id)
        )
        if live.task is not None and not live.task.done():
            return
        self._launch(live, definition, self._config(run_id))

    async def _stop(self, live: _Live) -> None:
        task, live.task = live.task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._release_all(live)

    async def _release_all(self, live: _Live) -> None:
        for execution_id, execution in tuple(live.executions.items()):
            execution.abandon()
            self._registry.release(live.run_id, execution_id)
            live.executions.pop(execution_id, None)

    def _acquire(
        self, live: _Live, execution_id: ExecutionId, node_id: NodeId
    ) -> NodeExecution:
        found = live.executions.get(execution_id)
        if found is not None:
            return found
        execution = NodeExecution(self, live.run_id, execution_id, node_id)
        live.executions[execution_id] = execution
        self._registry.register(live.run_id, execution_id, node_id, execution)
        return execution

    # --- reading position --------------------------------------------------

    def _config(
        self, run_id: RunId, checkpoint_id: CheckpointId | None = None
    ) -> dict[str, Any]:
        configurable: dict[str, Any] = {"thread_id": str(run_id), "checkpoint_ns": ""}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = str(checkpoint_id)
        return {"configurable": configurable}

    async def _require(self, run_id: RunId) -> RunRecord:
        record = await self._store.run(run_id)
        if record is None:
            raise UnknownRunError(f"unknown run: {run_id}")
        return record

    async def _definition_for(self, run_id: RunId) -> LangGraphDefinition:
        record = await self._require(run_id)
        definition = self._definitions.get(record.graph_id)
        if definition is None:
            raise UnknownGraphError(
                f"run {run_id} is of graph {record.graph_id}, which this runtime "
                "does not have"
            )
        return definition

    async def _state(self, definition: LangGraphDefinition, run_id: RunId) -> Any:
        return await definition.graph.aget_state(self._config(run_id))

    async def _position(
        self, definition: LangGraphDefinition, config: Mapping[str, Any]
    ) -> Checkpoint:
        return self._checkpoint(
            definition, await definition.graph.aget_state(dict(config))
        )

    def _checkpoint(self, definition: LangGraphDefinition, state: Any) -> Checkpoint:
        return Checkpoint(
            checkpoint_id=CheckpointId(state.config["configurable"]["checkpoint_id"]),
            parent_id=_parent_of(state.parent_config),
            next_nodes=self._frontier(definition, state.next),
            values=dict(state.values),
            source=_SOURCES.get(state.metadata.get("source", ""), "superstep"),
        )

    def _checkpoint_from_stream(
        self, definition: LangGraphDefinition, chunk: Mapping[str, Any]
    ) -> Checkpoint:
        metadata = chunk.get("metadata") or {}
        return Checkpoint(
            checkpoint_id=CheckpointId(chunk["config"]["configurable"]["checkpoint_id"]),
            parent_id=_parent_of(chunk.get("parent_config")),
            next_nodes=self._frontier(definition, chunk.get("next", ())),
            values=dict(chunk.get("values") or {}),
            source=_SOURCES.get(metadata.get("source", ""), "superstep"),
        )

    def _frontier(
        self, definition: LangGraphDefinition, nodes: Iterable[str]
    ) -> tuple[NodeId, ...]:
        """What a position would run next, with LangGraph's bookkeeping resolved.

        `__start__` is not somewhere a run goes; it is how Pregel spells "the
        input has not been written to the channels yet". A position holding it
        is about to run the graph's entry, and saying so is the truthful answer
        -- reporting `__start__` would name a node no topology has, and dropping
        it would claim the run had nowhere left to go.
        """
        frontier: list[NodeId] = []
        for name in nodes:
            if name == START:
                frontier.extend(definition.entry_points)
            else:
                frontier.append(NodeId(name))
        return tuple(dict.fromkeys(frontier))

    def _beyond(
        self, definition: LangGraphDefinition, running: Sequence[NodeId]
    ) -> tuple[NodeId, ...]:
        """Where a superstep that is still executing would go when it commits.

        LangGraph's `next` is the superstep *in flight* until it commits, and
        the contract asks a snapshot to look past that: while three reviewers
        are working, "what runs next" is the reranker, not the reviewers. So
        this reads the static topology, which is where a graph's shape is
        described and the only thing that can answer before the nodes have.

        Approximate where the edges out of a running node are conditional --
        every branch is listed, because which one is taken is not decided yet.
        Runtime routing stays LangGraph's; the moment the superstep commits the
        checkpoint answers instead, and it answers exactly.
        """
        topology = definition.topology
        return tuple(
            dict.fromkeys(
                edge.target for edge in topology.edges if edge.source in running
            )
        )

    async def _snapshot(self, run_id: RunId) -> RunSnapshot:
        record = await self._require(run_id)
        definition = self._definitions[record.graph_id]
        state = await self._state(definition, run_id)
        pending = await self._store.pending_approvals(run_id)
        active = self._registry.active(run_id)
        next_nodes = (
            self._beyond(definition, [one.node_id for one in active])
            if active
            else self._frontier(definition, state.next)
        )
        return RunSnapshot(
            run_id=run_id,
            graph_id=record.graph_id,
            status=_status(record.error, self._frontier(definition, state.next), pending),
            active_executions=active,
            next_nodes=next_nodes,
            checkpoint_id=CheckpointId(
                state.config["configurable"]["checkpoint_id"]
            ),
            values=dict(state.values),
            pending_approvals=tuple(_pending(record_) for record_ in pending),
            error=record.error,
        )


def _status(
    error: str, next_nodes: Sequence[NodeId], pending: Sequence[ApprovalRecord]
) -> RunStatus:
    """Derived rather than assigned, so a fan-out cannot contradict itself.

    With one reviewer waiting on a person and two working, the run is both, and
    the honest summary is the one a list view can show with the approvals
    underneath it.
    """
    if error:
        return RunStatus.FAILED
    if not next_nodes:
        return RunStatus.COMPLETED
    if pending:
        return RunStatus.AWAITING_APPROVAL
    return RunStatus.RUNNING


def _pending(record: ApprovalRecord) -> PendingApproval:
    return PendingApproval(
        approval_id=record.approval_id,
        execution_id=record.execution_id,
        node_id=record.node_id,
        kind=record.kind,
        reason=record.reason,
        command=record.command,
        tool_name=record.tool_name,
        allowed_decisions=record.allowed_decisions,
    )


def _parent_of(config: Mapping[str, Any] | None) -> CheckpointId | None:
    if not config:
        return None
    parent = config.get("configurable", {}).get("checkpoint_id")
    return CheckpointId(parent) if parent else None


__all__ = ["LangGraphRuntime"]
