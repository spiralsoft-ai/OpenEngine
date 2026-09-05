"""ACP agents under the graph runtime, including losing the process that ran one.

`tests/test_graph_runtime.py` already drives the whole control surface against
this binding -- linear graphs, fan-out, the same node running three times at
once, checkpoint history, fork and resume, shutdown. What is here is the part
that needs a real agent on the other end of a real pipe:

* steering an ACP execution without it becoming a second conversation;
* an approval request that outlives the Python task that raised it;
* the same, when the runtime is destroyed and rebuilt from files in between;
* a refusal;
* several agents asking at once, each answered separately;
* durability backed by a LangGraph SQLite checkpointer rather than a dict.

The agent is `tests/acp_stub_agent.py`, launched as a child process. It keeps
its own conversation on disk and remembers an unanswered permission request
across a reload, which is what a real agent mid-tool-call does -- and what makes
a broken handoff fail here rather than pass quietly.

Every durability test writes to `tmp_path` and rebuilds *everything* from it: a
new `LangGraphRuntime`, a new checkpointer connection, a new store, and a new
agent process. Nothing but the files crosses the boundary, which is the only
version of this test worth having.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from engine.domain import ApprovalDecision, RunId
from engine.graph_runtime import EventLog, GraphId, NodeId, RuntimeEvent
from engine.graph_runtime_langgraph import (
    LangGraphDefinition,
    LangGraphRuntime,
    SqliteGraphRuntimeStore,
    answer_permission,
)
from engine.graph_runtime_langgraph.acp import APPROVAL_ID, ACPNode
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

from acp_stub_agent import ASK_NARRATION, DONE, NARRATED_TOOL, NARRATION
from graph_runtime_backends import State

STUB = Path(__file__).parent / "acp_stub_agent.py"
GRAPH = GraphId("acp-review")
IMPLEMENTATION = NodeId("implementation")
REVIEW = NodeId("review")
AGENT = "stub"
PROMPT = "Implement the feature and run the tests."

#: Long enough that only a genuinely stuck run reaches it. A passing run never
#: waits, but a child process and two SQLite files make this slower than the
#: in-memory suite.
PATIENCE = 30.0


def registry(
    tmp_path: Path,
    *,
    asks: bool = False,
    response: str = DONE,
    narrates: bool = False,
) -> ACPAgentRegistry:
    """One stub agent, reachable as `"stub"`, answering through the runtime."""
    return ACPAgentRegistry(
        [
            StdioACPProvider(
                name=AGENT,
                command=[sys.executable, str(STUB)],
                env={
                    "STUB_ACP_STATE": str(tmp_path),
                    "STUB_ACP_LOG": str(tmp_path / "agent.log"),
                    "STUB_ACP_RESPONSE": response,
                    **({"STUB_ACP_ASK": "1"} if asks else {}),
                    **({"STUB_ACP_NARRATE": "1"} if narrates else {}),
                },
                # The seam the whole design turns on: a permission request comes
                # in on the ACP connection, and this is what routes it back to
                # the execution that owns the conversation.
                permissions=answer_permission,
            )
        ]
    )


def pipeline(
    saver: Any, agents: ACPAgentRegistry, where: Path
) -> LangGraphDefinition:
    """implementation -> review, with the implementation node an ACP agent."""
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        str(IMPLEMENTATION),
        ACPNode(agent=AGENT, prompt=PROMPT, registry=agents, cwd=str(where)),
    )
    builder.add_node(str(REVIEW), _reviewed)
    builder.add_edge(START, str(IMPLEMENTATION))
    builder.add_edge(str(IMPLEMENTATION), str(REVIEW))
    builder.add_edge(str(REVIEW), END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP review", graph=builder.compile(checkpointer=saver)
    )


def pool(saver: Any, agents: ACPAgentRegistry, where: Path) -> LangGraphDefinition:
    """Two ACP agents at once, which is what makes routing an answer a question."""
    builder: StateGraph = StateGraph(State)
    for node_id in ("agent-1", "agent-2"):
        builder.add_node(
            node_id,
            ACPNode(
                agent=AGENT,
                prompt=f"{PROMPT} ({node_id})",
                registry=agents,
                cwd=str(where),
            ),
        )
        builder.add_edge(START, node_id)
        builder.add_edge(node_id, END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP pool", graph=builder.compile(checkpointer=saver)
    )


async def _reviewed(_state: dict[str, Any]) -> dict[str, Any]:
    return {str(REVIEW): "Looks right."}


@asynccontextmanager
async def runtime_over(
    tmp_path: Path,
    agents: ACPAgentRegistry,
    build: Any = pipeline,
) -> AsyncIterator[tuple[LangGraphRuntime, EventLog]]:
    """A runtime built entirely from what is on disk, and closed like a server.

    Everything durable is a file under `tmp_path`, so entering this twice is a
    process restart in every sense that matters: a second checkpointer
    connection, a second store, and a runtime that has never seen the run it is
    about to be asked about.
    """
    store = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "checkpoints.db")
    ) as saver:
        runtime = LangGraphRuntime(build(saver, agents, tmp_path), store=store)
        log = EventLog()
        runtime.observe(log.append)
        try:
            yield runtime, log
        finally:
            await runtime.aclose()
            store.close()


async def until(
    log: EventLog, run_id: RunId, kind: str, count: int = 1, cursor: int = 0
) -> list[RuntimeEvent]:
    """Everything up to and including the `count`th event of `kind`."""
    seen: list[RuntimeEvent] = []
    async with asyncio.timeout(PATIENCE):
        async for event in log.stream(run_id, cursor):
            seen.append(event)
            if event.kind.value == kind:
                count -= 1
                if count == 0:
                    return seen
    raise AssertionError("unreachable")  # pragma: no cover


def transcript(events: Sequence[RuntimeEvent]) -> list[tuple[str, str]]:
    return [
        (str(event.payload["role"]), str(event.payload["text"]))
        for event in events
        if event.kind.value == "transcript"
    ]


#: Which field names the thing each kind of activity is about.
_NAMED_BY = {
    "transcript": "text",
    "tool.call": "name",
    "tool.result": "name",
    "approval.requested": "reason",
}


def activity(events: Sequence[RuntimeEvent], node_id: NodeId) -> list[tuple[str, str]]:
    """What one node did, in order: what it said, called, and asked for."""
    return [
        (event.kind.value, str(event.payload[_NAMED_BY[event.kind.value]]))
        for event in events
        if event.kind.value in _NAMED_BY and event.node_id == node_id
    ]


def sessions(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Every conversation the agent kept, as the agent left it on disk."""
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(tmp_path.glob("sess_*.json"))
    }


def sent(tmp_path: Path, method: str) -> list[dict[str, Any]]:
    """Every call of `method` the agent was sent, across every process."""
    log = tmp_path / "agent.log"
    return [
        message
        for message in (
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        )
        if message.get("method") == method
    ]


def prompts(tmp_path: Path) -> list[str]:
    return [
        "".join(
            str(block.get("text", ""))
            for block in message["params"].get("prompt", [])
            if isinstance(block, dict)
        )
        for message in sent(tmp_path, "session/prompt")
    ]


# --- an agent that just runs ------------------------------------------------


def test_an_acp_node_runs_a_turn_and_publishes_what_happened(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[RuntimeEvent], dict[str, Any]]:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            events = await until(log, run.run_id, "run.finished")
            final = await runtime.snapshot(run.run_id)
            return events, dict(final.values)

    events, values = asyncio.run(scenario())

    # Both halves of the turn: what the node was sent to do, and what it said
    # about doing it. A transcript holding only the second is not a
    # conversation, and a reader opening one has to guess what was asked.
    assert transcript(events) == [("user", PROMPT), ("assistant", DONE)]
    assert values == {str(IMPLEMENTATION): DONE, str(REVIEW): "Looks right."}
    started = [event for event in events if event.kind.value == "conversation.started"]
    assert len(started) == 1
    assert started[0].node_id == IMPLEMENTATION
    assert started[0].payload == {
        "agent": AGENT,
        "sessionId": next(iter(sessions(tmp_path))),
        "resumed": False,
    }
    # One conversation, started once. The node did not open a second.
    assert len(sent(tmp_path, "session/new")) == 1
    assert prompts(tmp_path) == [PROMPT]


def test_what_an_agent_says_is_published_where_it_said_it(tmp_path: Path) -> None:
    """A line written before a tool call is published before that call.

    An agent narrates as it works -- a sentence, a tool call, the next
    sentence -- and a reader following along needs the sentence that explains a
    call to arrive before it. Holding every word until the turn ends would put
    the whole narration after all of the work it describes, which reads as an
    agent that did a pile of things silently and then summarized them.
    """

    async def scenario() -> list[RuntimeEvent]:
        async with runtime_over(tmp_path, registry(tmp_path, narrates=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            return await until(log, run.run_id, "run.finished")

    events = asyncio.run(scenario())

    assert activity(events, IMPLEMENTATION) == [
        ("transcript", PROMPT),
        ("transcript", NARRATION),
        ("tool.call", NARRATED_TOOL),
        ("tool.result", NARRATED_TOOL),
        ("transcript", DONE),
    ]


def test_a_line_explaining_a_request_is_published_before_the_wait(
    tmp_path: Path,
) -> None:
    """And a permission request is where that matters most.

    It is the one point where a turn stops for as long as a person takes to
    answer, and the sentence saying why the agent is asking is written just
    before it. Held to the end of the turn, that sentence would be published
    only once somebody had answered -- so for the whole time the run was
    genuinely waiting on them, the conversation would be empty.
    """

    async def scenario() -> list[RuntimeEvent]:
        async with runtime_over(
            tmp_path, registry(tmp_path, asks=True, narrates=True)
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            # Everything published up to the question and no further. The
            # answer is given only after this, so whatever is in here arrived
            # while the run was still blocked on a person.
            asked = await until(log, run.run_id, "approval.requested")
            await runtime.decide(
                run.run_id,
                asked[-1].payload["approvalId"],  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            await until(log, run.run_id, "run.finished")
            return asked

    waiting = asyncio.run(scenario())

    assert activity(waiting, IMPLEMENTATION) == [
        ("transcript", PROMPT),
        ("transcript", ASK_NARRATION),
        ("approval.requested", "run the tests"),
    ]


# --- steering ---------------------------------------------------------------


def test_steering_an_acp_execution_continues_the_same_session(tmp_path: Path) -> None:
    """The requirement, stated as what must *not* have happened.

    A message for an agent that is already running is not a question about what
    the graph should run next. So: no second `session/new`, no second entry into
    the node, and the instruction delivered as a further turn of the
    conversation the agent was already in.
    """

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            approval = asked[-1]
            # Steered while the agent is blocked on a person. A runtime that had
            # suspended the graph node to ask would have nothing to deliver to.
            waiting = await runtime.snapshot(run.run_id)
            await runtime.steer(run.run_id, "Use the fast suite.")
            await runtime.decide(
                run.run_id,
                approval.payload["approvalId"],  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "waiting": waiting,
                "events": events,
                "entered": runtime.entered(IMPLEMENTATION),
            }

    outcome = asyncio.run(scenario())

    assert outcome["waiting"].status.value == "awaiting_approval"
    assert [one.node_id for one in outcome["waiting"].active_executions] == [
        IMPLEMENTATION
    ]
    assert outcome["entered"] == 1
    assert transcript(outcome["events"]) == [
        ("user", PROMPT),
        ("assistant", DONE),
        ("user", "Use the fast suite."),
        ("assistant", DONE),
    ]
    # One conversation, two turns in it: the instruction reached the agent that
    # was already running rather than starting a second one.
    assert len(sent(tmp_path, "session/new")) == 1
    assert prompts(tmp_path) == [PROMPT, "Use the fast suite."]
    assert list(sessions(tmp_path).values())[0]["turns"] == [
        PROMPT,
        "Use the fast suite.",
    ]


# --- approvals, answered by the process that raised them --------------------


def test_an_acp_permission_request_becomes_an_answerable_approval(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested")
            paused = await runtime.snapshot(run.run_id)
            approval = paused.pending_approvals[0]
            stored = await runtime.store.approval(approval.approval_id)
            released = await runtime.decide(
                run.run_id, approval.approval_id, ApprovalDecision.ACCEPT
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "paused": paused,
                "approval": approval,
                "stored": stored,
                "released": released,
                "events": events,
            }

    outcome = asyncio.run(scenario())
    approval = outcome["approval"]
    stored = outcome["stored"]

    # The question, as the agent described it and a person would read it.
    assert approval.node_id == IMPLEMENTATION
    assert approval.reason == "run the tests"
    assert approval.command == "pytest"
    assert approval.execution_id in {
        one.execution_id for one in outcome["paused"].active_executions
    }
    # And, beside it, everything needed to reach the conversation again --
    # written down before the wait, not after it.
    assert stored.continuation is not None
    assert stored.continuation.agent == AGENT
    assert stored.continuation.session_id in sessions(tmp_path)
    assert stored.continuation.thread_id == str(outcome["paused"].run_id)
    assert outcome["released"].pending_approvals == ()
    assert transcript(outcome["events"]) == [("user", PROMPT), ("assistant", DONE)]
    # The call the question was about, in the agent's own ids, so a client can
    # draw the question beside the command rather than beside the whole turn.
    requested = next(
        event
        for event in outcome["events"]
        if event.kind.value == "approval.requested"
    )
    assert requested.payload["toolCallId"] == "call_1"


def test_refusing_an_acp_approval_stops_the_run_where_it_asked(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            approval = asked[-1].payload["approvalId"]
            refused = await runtime.decide(
                run.run_id, approval, ApprovalDecision.CANCEL  # type: ignore[arg-type]
            )
            events = await until(log, run.run_id, "run.failed")
            return {"refused": refused, "events": events}

    outcome = asyncio.run(scenario())
    refused = outcome["refused"]

    assert refused.status.value == "failed"
    assert refused.error == "run the tests was not allowed"
    assert refused.active_executions == ()
    # The position is untouched: the superstep never committed, so the run can
    # be sent back and tried again.
    assert refused.next_nodes == (IMPLEMENTATION,)
    failed = [event for event in outcome["events"] if event.kind.value == "run.failed"]
    assert failed[-1].node_id == IMPLEMENTATION


def test_two_agents_asking_at_once_are_answered_separately(tmp_path: Path) -> None:
    """The reason an approval carries an execution id rather than a node name."""

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested", 2)
            waiting = await runtime.snapshot(run.run_id)
            first, second = waiting.pending_approvals
            after_one = await runtime.decide(
                run.run_id, first.approval_id, ApprovalDecision.ACCEPT
            )
            await runtime.decide(
                run.run_id, second.approval_id, ApprovalDecision.ACCEPT
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "waiting": waiting,
                "first": first,
                "second": second,
                "after_one": after_one,
                "events": events,
            }

    outcome = asyncio.run(scenario())
    first, second = outcome["first"], outcome["second"]

    assert {first.node_id, second.node_id} == {NodeId("agent-1"), NodeId("agent-2")}
    assert first.execution_id != second.execution_id
    assert first.approval_id != second.approval_id
    # Answering one released one. The other agent is still waiting.
    assert [one.approval_id for one in outcome["after_one"].pending_approvals] == [
        second.approval_id
    ]
    resolved = [
        event
        for event in outcome["events"]
        if event.kind.value == "approval.resolved"
    ]
    assert [event.execution_id for event in resolved[:2]] == [
        first.execution_id,
        second.execution_id,
    ]
    # Two agents, two conversations, neither answered on the other's behalf.
    assert len(sessions(tmp_path)) == 2
    assert all(session["granted"] for session in sessions(tmp_path).values())


# --- approvals answered by a process that never raised them -----------------


def test_an_approval_survives_the_runtime_that_raised_it(tmp_path: Path) -> None:
    """The whole point of the handoff, tested by throwing the process away.

    The first runtime raises the request and is then destroyed -- driver task,
    ACP connection, agent process and all. The second is built from nothing but
    the files: a new checkpointer connection, a new store, and a graph it has
    never run. It answers the request, and the agent picks up the conversation
    it was already in.

    What must be true afterwards is the part that makes this different from a
    pause and a resume: one `session/new` across both processes, one delivery of
    the original prompt, and a `session/load` in between. Any of those going the
    other way would mean the agent had been handed its own work again as a fresh
    task.
    """

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_it(run_id: RunId, approval_id: str) -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            found = await runtime.snapshot(run_id)
            released = await runtime.decide(
                run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            events = await until(log, run_id, "run.finished")
            return {
                "found": found,
                "released": released,
                "events": events,
                "final": await runtime.snapshot(run_id),
            }

    run_id, approval_id = asyncio.run(raise_it())
    # Nothing is alive between these two lines. Whatever the second runtime
    # knows, it read off the disk.
    outcome = asyncio.run(answer_it(run_id, approval_id))

    found = outcome["found"]
    assert found is not None
    assert found.status.value == "awaiting_approval"
    assert [one.approval_id for one in found.pending_approvals] == [approval_id]
    # Nothing was executing: the task that asked died with its process, which is
    # exactly the state the handoff exists to be answerable from.
    assert found.active_executions == ()
    assert found.next_nodes == (IMPLEMENTATION,)

    assert outcome["final"].status.value == "completed"
    assert outcome["final"].values[str(IMPLEMENTATION)] == DONE
    assert transcript(outcome["events"])[-1] == ("assistant", DONE)

    # One conversation, across two processes.
    assert len(sent(tmp_path, "session/new")) == 1
    assert len(sent(tmp_path, "session/load")) == 1
    session = list(sessions(tmp_path).values())[0]
    assert session["loads"] == 1
    assert session["granted"] is True
    # The original prompt was delivered once. The second turn is the
    # continuation, which says only that the question was answered.
    assert prompts(tmp_path).count(PROMPT) == 1
    assert len(prompts(tmp_path)) == 2
    assert PROMPT not in prompts(tmp_path)[1]


def test_two_lost_approvals_are_both_answered_before_anything_restarts(
    tmp_path: Path,
) -> None:
    """A superstep is plural, so a lost process can leave several mid-question.

    Restarting on the first answer would re-enter every node of the superstep,
    including the one still waiting: it has no decision to apply, so it would
    open a second conversation and put its original prompt to it as fresh work,
    and the answer sent afterwards would arrive for an execution that no longer
    exists. So the first decision is durable and inert, and the restart happens
    once, when nothing is left unanswered.
    """

    async def raise_them() -> tuple[RunId, list[str]]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested", 2)
            waiting = await runtime.snapshot(run.run_id)
            return run.run_id, [
                str(one.approval_id) for one in waiting.pending_approvals
            ]

    async def answer_them(run_id: RunId, approvals: list[str]) -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            first, second = approvals
            after_one = await runtime.decide(
                run_id, first, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            # Nothing may be moving yet. A driver started here would be running
            # the agent whose question is still on somebody's screen.
            await asyncio.sleep(0.2)
            resting = {
                "snapshot": await runtime.snapshot(run_id),
                "running": list(runtime.running()),
                "sessions": len(sent(tmp_path, "session/new")),
            }
            await runtime.decide(
                run_id, second, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run_id, "run.finished")
            return {
                "after_one": after_one,
                "resting": resting,
                "final": await runtime.snapshot(run_id),
            }

    run_id, approvals = asyncio.run(raise_them())
    assert len(approvals) == 2
    outcome = asyncio.run(answer_them(run_id, approvals))

    # One answered, one outstanding, and the run still going nowhere.
    assert [str(one.approval_id) for one in outcome["after_one"].pending_approvals] == [
        approvals[1]
    ]
    assert outcome["resting"]["running"] == []
    assert outcome["resting"]["snapshot"].status.value == "awaiting_approval"
    assert outcome["resting"]["sessions"] == 2

    assert outcome["final"].status.value == "completed"
    # Two conversations, started once each in the first process and reloaded
    # once each in the second. A third `session/new` would be an agent handed
    # its own outstanding work as a new task.
    assert len(sent(tmp_path, "session/new")) == 2
    assert len(sent(tmp_path, "session/load")) == 2
    assert len(sessions(tmp_path)) == 2
    assert all(session["granted"] for session in sessions(tmp_path).values())
    assert all(session["loads"] == 1 for session in sessions(tmp_path).values())
    for node_id in ("agent-1", "agent-2"):
        assert prompts(tmp_path).count(f"{PROMPT} ({node_id})") == 1


def test_a_reconstructed_runtime_refuses_an_approval_it_already_answered(
    tmp_path: Path,
) -> None:
    """Answering twice is a race that lost, not a request that never existed."""

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_twice(run_id: RunId, approval_id: str) -> str:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            await runtime.decide(
                run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run_id, "run.finished")
            with pytest.raises(Exception) as refused:
                await runtime.decide(
                    run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
                )
            return type(refused.value).__name__

    run_id, approval_id = asyncio.run(raise_it())
    assert asyncio.run(answer_twice(run_id, approval_id)) == "ApprovalNotPendingError"


def test_an_answered_approval_does_not_follow_the_node_into_its_next_entry(
    tmp_path: Path,
) -> None:
    """A continuation names a question only while there is one outstanding.

    An approval answered without the process ever going away is not something
    to come back to, and leaving it on the stored continuation is worse than
    useless. The next entry into the node -- a fork here, but a loop would do --
    would find a settled decision waiting, resume a conversation nobody is
    holding, send the continuation prompt in place of its own, and auto-answer
    the first permission request it met with an answer given to a different
    question entirely.
    """

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            first = str(asked[-1].payload["approvalId"])
            await runtime.decide(
                run.run_id, first, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            done = await until(log, run.run_id, "run.finished")
            binding = await runtime.store.session(run.run_id, str(IMPLEMENTATION))
            history = await runtime.history(run.run_id)
            await runtime.resume_from(run.run_id, history[0].checkpoint_id)
            # The re-attempt has to ask again. Being answered without asking is
            # exactly the failure this is here for.
            again = await until(
                log, run.run_id, "approval.requested", cursor=done[-1].sequence
            )
            second = str(again[-1].payload["approvalId"])
            await runtime.decide(
                run.run_id, second, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run.run_id, "run.finished", cursor=again[-1].sequence)
            return {"binding": binding, "first": first, "second": second}

    outcome = asyncio.run(scenario())

    assert APPROVAL_ID not in outcome["binding"].metadata
    assert outcome["second"] != outcome["first"]
    # A fresh conversation for the re-attempt, given the node's own prompt. The
    # abandoned one is not reloaded: replaying an attempt is a different
    # attempt, and continuing the old session would hand the agent its own
    # discarded work as context.
    assert len(sent(tmp_path, "session/new")) == 2
    assert sent(tmp_path, "session/load") == []
    assert prompts(tmp_path) == [PROMPT, PROMPT]


# --- durability of position, not just of the question -----------------------


def test_history_and_forks_survive_a_new_runtime(tmp_path: Path) -> None:
    """A checkpoint is LangGraph's, so it outlives whoever was driving it."""

    async def first() -> RunId:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.finished")
            return run.run_id

    async def second(run_id: RunId) -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            before = await runtime.history(run_id)
            forked = await runtime.resume_from(run_id, before[0].checkpoint_id)
            await until(log, run_id, "run.finished")
            return {
                "before": before,
                "forked": forked,
                "after": await runtime.history(run_id),
                "entered": runtime.entered(IMPLEMENTATION),
            }

    run_id = asyncio.run(first())
    outcome = asyncio.run(second(run_id))
    before, after = outcome["before"], outcome["after"]

    assert [point.source for point in before] == ["start", "superstep", "superstep"]
    assert after[: len(before)] == before
    fork = after[len(before)]
    assert fork.source == "fork"
    assert fork.parent_id == before[0].checkpoint_id
    assert fork.checkpoint_id == outcome["forked"].checkpoint_id
    # A fork re-attempts the superstep, so the node ran again -- in this process,
    # which had never run it before.
    assert outcome["entered"] == 1
    # And a fresh conversation, because a replayed attempt is a different
    # attempt: continuing the abandoned one would hand the agent its own
    # discarded work as context.
    assert len(sent(tmp_path, "session/new")) == 2


# --- shutdown ---------------------------------------------------------------


def test_shutdown_leaves_no_task_and_no_agent_behind(tmp_path: Path) -> None:
    """A leaked task looks exactly like a node that is thinking. So: count them.

    The agent is left mid-request on purpose -- blocked on a permission nobody
    is going to answer -- because that is the shutdown that goes wrong: a
    subprocess holding a pipe open and a coroutine waiting on a future.
    """

    async def scenario() -> tuple[int, int, list[RunId]]:
        before = len(asyncio.all_tasks())
        agents = registry(tmp_path, asks=True)
        store = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
        async with AsyncSqliteSaver.from_conn_string(
            str(tmp_path / "checkpoints.db")
        ) as saver:
            runtime = LangGraphRuntime(pipeline(saver, agents, tmp_path), store=store)
            log = EventLog()
            runtime.observe(log.append)
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested")
            await runtime.aclose()
            still_running = list(runtime.running())
        store.close()
        # Two turns of the loop for the cancelled tasks to finish unwinding;
        # cancellation is delivered, not applied, at the moment it is requested.
        for _ in range(5):
            await asyncio.sleep(0)
        return before, len(asyncio.all_tasks()), still_running

    before, after, still_running = asyncio.run(scenario())

    assert still_running == []
    assert after <= before


# --- where the agent works --------------------------------------------------


def working_in(
    saver: Any, agents: ACPAgentRegistry, _where: Path
) -> LangGraphDefinition:
    """One agent node, told to work wherever the run's state says."""
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        str(IMPLEMENTATION),
        ACPNode(
            agent=AGENT,
            prompt=PROMPT,
            registry=agents,
            cwd=lambda state: str(state.get("workspace") or ""),
        ),
    )
    builder.add_edge(START, str(IMPLEMENTATION))
    builder.add_edge(str(IMPLEMENTATION), END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP cwd", graph=builder.compile(checkpointer=saver)
    )


def test_a_node_works_in_the_directory_its_run_was_given(tmp_path: Path) -> None:
    """The checkout is the run's, so one graph has to serve every run.

    A `cwd` fixed when the graph was written would mean a definition per
    checkout -- or every run of every graph sharing one working tree, which is
    the same bug with fewer objects. Read off the state the run was started
    with, and asserted where it actually lands: the `session/new` the agent was
    sent.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    async def scenario() -> None:
        async with runtime_over(tmp_path, registry(tmp_path), working_in) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {"workspace": str(checkout)})
            await until(log, run.run_id, "run.finished")

    asyncio.run(scenario())

    opened = sent(tmp_path, "session/new")
    assert [message["params"]["cwd"] for message in opened] == [str(checkout)]


def test_a_node_that_resolves_no_directory_starts_no_agent(tmp_path: Path) -> None:
    """The failure that must never be quiet: a run with nowhere to work.

    Starting the same graph without the state its resolver reads is not exotic
    -- a `WorkspaceNode` omitted or ordered after the agent, a provider that
    answered with an empty path, a fork re-entering this node from a position
    taken before anything was provisioned -- and ACP resolves an absent working
    directory against the *client's* process. So the quiet outcome here is an
    agent editing the server's own checkout, with nothing in the run saying so.

    Asserted against the agent's own log, which does not exist: the stub never
    ran, so no session was opened in the server's directory or in any other. A
    weaker check -- that the `cwd` sent was not the server's -- would pass for a
    run that started an agent somewhere else nobody chose.
    """

    async def scenario() -> Any:
        async with runtime_over(tmp_path, registry(tmp_path), working_in) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id)

    failed = asyncio.run(scenario())

    assert failed.status.value == "failed"
    assert "no working directory" in failed.error
    assert not (tmp_path / "agent.log").exists()
