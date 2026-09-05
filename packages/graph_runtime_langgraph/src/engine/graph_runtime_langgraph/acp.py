"""An ACP agent as a LangGraph node, controllable while it runs.

    GraphRuntime.steer(execution_id)   -> NodeExecution -> ACPSession
    GraphRuntime.decide(approval_id)   -> ExecutionId   -> NodeExecution
                                                        -> ACPSession

Both arrows stop at the session. The graph node is not interrupted, suspended or
re-entered to carry either: an instruction becomes a further turn in the *same*
conversation, and an answer to a permission request resolves the future the
session's own handler is sitting on. That is why execution-level control is not
a LangGraph interrupt -- an interrupt ends the task, and the conversation the
agent was in the middle of goes with it.

## Relinquishing the process

An agent asking to run a command is not a reason to keep a worker alive. The
person may answer in a minute or on Monday, and a coroutine holding a subprocess
open in between is both expensive and fragile.

So when the agent asks, this node writes down everything needed to come back --
the `ACPContinuation` that names the conversation, and the approval id that says
which question it is waiting on -- and only then waits. If the process dies
there, nothing is lost. LangGraph's checkpoint still has the superstep
uncommitted, the store still has the question, and `decide()` in a process that
has never seen this run writes the answer down and starts the thread again:

    reload the thread -> LangGraph re-enters this node
        -> resume_continuation() -> session/load
        -> the agent asks again -> the persisted answer is applied
        -> the same conversation carries on

The agent is never handed the original prompt a second time. That distinction is
the point: replaying the prompt would be a new task that happened to look like
the old one, with the reasoning and tool history the agent had built up thrown
away. What it gets instead is `continuation_prompt`, which says only that the
outstanding request has been answered.

Reconnecting is `langgraph-acp`'s: `resume_continuation` is its call and
`ACPContinuation` is its record, stored verbatim. Nothing here re-derives what a
session id means or how a connection reloads one.

A session is resumed only when there is an answer to apply. Anywhere else --
including a fork, which re-attempts a superstep from scratch -- a fresh
conversation is the honest one: the attempt being replayed is a different
attempt, and continuing the abandoned one would hand the agent its own rejected
work as context.

## Where permission answers arrive

A `session/request_permission` arrives on the ACP connection rather than on the
graph, so it has to be routed back to the execution that owns the conversation.
The handler is `answer_permission`, and it is given to the provider:

    StdioACPProvider(name="codex", command=[...], permissions=answer_permission)

Routing is by ACP session id, which is why the table it looks in is module-level
rather than per-runtime: a session id is the agent's and unique across every
connection, while the provider holding the handler is configured before any
runtime exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind
from langgraph_acp import (
    ACPAgentRegistry,
    ACPContinuation,
    ACPEventType,
    ACPPermissionOutcome,
    ACPPermissionRequest,
    ACPPrompt,
    ACPSession,
    default_registry,
    resume_continuation,
)

from engine.graph_runtime.events import EventKind
from engine.graph_runtime_langgraph.executions import NodeExecution, current_execution

#: Which turn each live ACP conversation belongs to, keyed by the agent's own
#: session id. Module-level because a provider is configured long before a
#: runtime is, and the key is globally unique so nothing can collide.
_TURNS: dict[str, "_Turn"] = {}

#: The events that mean the agent has stopped writing and started doing, and so
#: that whatever it has said so far is a finished thought worth publishing. See
#: `ACPNode._speak`.
_INTERRUPTS_THE_NARRATION = frozenset(
    {ACPEventType.TOOL_STARTED, ACPEventType.PERMISSION_REQUESTED}
)

#: What a continuation carries for this package, under `ACPContinuation.metadata`.
#: Flat names rather than a nested object: this ends up as JSON in somebody
#: else's store, and a flat record is the one that survives being read by hand.
APPROVAL_ID = "graph_runtime.approval_id"
EXECUTION_ID = "graph_runtime.execution_id"
NODE_ID = "graph_runtime.node_id"
RUN_ID = "graph_runtime.run_id"


async def answer_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Answer `session/request_permission` for whichever execution owns it.

    Installed on the provider, and the only ACP-shaped thing on the far side of
    the generic runtime. A request naming a session nobody in this process is
    driving is declined: approving on behalf of a conversation we are not
    holding would approve something nobody was shown.
    """
    turn = _TURNS.get(request.session_id) if request.session_id is not None else None
    if turn is None:
        return ACPPermissionOutcome.cancelled()
    return await turn.ask(request)


@dataclass(slots=True)
class _Turn:
    """One node invocation's ACP state, as the permission handler needs it.

    Separate from `NodeExecution`, which is the generic contract -- two methods,
    nothing about agents. This is everything about one agent turn.
    """

    node: "ACPNode"
    execution: NodeExecution
    session_key: str
    session_id: str = ""
    answer: ApprovalDecision | None = None
    """An answer given before this process existed. Applied once, then cleared."""
    answered: ApprovalId | None = None
    narrating: Callable[[], Awaitable[None]] | None = None
    """`_speak`'s buffer flush, for as long as a turn is in flight.

    Called from here rather than only from the loop that fills the buffer,
    because these are two tasks: the events of a turn are consumed by `_speak`,
    while a permission request is answered on a task of the connection's own.
    Publishing the words from the task that raises the question is what puts
    them *before* it without depending on how the two get scheduled.
    """

    async def ask(self, request: ACPPermissionRequest) -> ACPPermissionOutcome:
        """Turn an ACP permission request into a runtime approval, or apply one.

        The first branch is resumption. The agent has reloaded the conversation
        and is asking the same question again, and the answer is already known;
        asking the person twice for one command is exactly what a handoff that
        had not really worked would look like.
        """
        # Whatever the agent said on its way to asking, published before
        # anything about the question is. See `narrating`.
        if self.narrating is not None:
            await self.narrating()
        if self.answer is not None:
            decision, self.answer = self.answer, None
            await self._settle()
            await self.execution.emit(
                EventKind.APPROVAL_RESOLVED,
                {
                    "approvalId": str(self.answered or ""),
                    "decision": decision.value,
                    "resumed": True,
                },
            )
            return _outcome(decision, request)
        approval_id = ApprovalId(f"approval-{uuid4().hex[:12]}")
        continuation = ACPContinuation(
            agent=self.node.agent,
            session_id=self.session_id,
            thread_id=str(self.execution.run_id),
            session_key=self.session_key,
            metadata={
                RUN_ID: str(self.execution.run_id),
                NODE_ID: str(self.execution.node_id),
                EXECUTION_ID: str(self.execution.execution_id),
                APPROVAL_ID: str(approval_id),
            },
        )
        # Bound to the run before the wait, not after: this is what a process
        # that never saw the question reads to find the conversation again, and
        # writing it afterwards would leave a window where dying loses it.
        await self.execution.runtime.store.remember_session(
            self.execution.run_id, self.session_key, continuation
        )
        decision = await self.execution.ask(
            reason=self.node.reason_for(request),
            kind=self.node.kind,
            command=self.node.command_of(request),
            tool_name=self.node.tool_of(request),
            session_key=self.session_key,
            continuation=continuation,
            request=dict(request.params),
            approval_id=approval_id,
            tool_call_id=self.node.call_of(request),
        )
        # Answered without the process ever going away, so the continuation has
        # done its job and the approval comes back off it. Deliberately not in a
        # `finally`: an `ask` that does not return is a process being taken away
        # mid-question, and that is exactly when the record has to survive.
        await self._settle()
        return _outcome(decision, request)

    async def _settle(self) -> None:
        """Drop the approval from the binding, keeping the conversation.

        A continuation naming an approval means "this session is mid-question,
        and here is the question". Once it is answered that stops being true,
        and leaving it written down is worse than useless: the next entry into
        this node would find a settled decision waiting, resume a conversation
        nobody is holding, send the continuation prompt in place of the node's
        real one, and auto-answer the first permission request it met with an
        answer given to a different question entirely.
        """
        await self.execution.runtime.store.remember_session(
            self.execution.run_id,
            self.session_key,
            ACPContinuation(
                agent=self.node.agent,
                session_id=self.session_id,
                thread_id=str(self.execution.run_id),
                session_key=self.session_key,
            ),
        )


def prompt_text(prompt: ACPPrompt) -> str:
    """The words in a prompt, for the transcript that records it being sent.

    A prompt is either a string or the content blocks ACP carries, and only the
    text of those is worth writing down: an image or a resource link is part of
    the request but not part of what a reader is reading.
    """
    if isinstance(prompt, str):
        return prompt
    return "".join(
        str(block.get("text", ""))
        for block in prompt
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _outcome(
    decision: ApprovalDecision, request: ACPPermissionRequest
) -> ACPPermissionOutcome:
    """The agent's own option that a runtime decision means.

    ACP answers are option ids the agent offered, not a vocabulary this package
    owns, so accepting means naming one of them -- a permissive one where the
    agent classified its options, and otherwise the first, which is where every
    agent in circulation puts it. Refusing needs no option at all, which is why
    cancelling is the answer that always exists.
    """
    if decision is ApprovalDecision.CANCEL or not request.options:
        return ACPPermissionOutcome.cancelled()
    allowing = next(
        (option for option in request.options if option.kind.startswith("allow")),
        request.options[0],
    )
    return ACPPermissionOutcome.selected(allowing.option_id)


class NoWorkingDirectoryError(ValueError):
    """An agent was about to be started without being told where to work.

    Loud on purpose, because the quiet alternative is the worst outcome this
    package can produce. ACP resolves an absent working directory against the
    *client's* process -- `os.path.abspath(os.getcwd())` -- so a node that
    reached a session with nothing would get one rooted in the server's own
    checkout, and an agent with permission to edit would begin editing the
    operator's repository. Nothing in the run would say so: there is no event
    for "started somewhere unintended", and the transcript of an agent working
    in the wrong tree reads exactly like one working in the right tree.

    So no directory is never a default here and never a fallback. It is a
    refusal, at the two moments it can be caught: when a workflow is written,
    and when a run resolves one.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ACPNode:
    """A LangGraph node that runs one ACP turn under the graph runtime.

    An async callable rather than something LangGraph has to know about, which
    is what `langgraph_acp.ACPNode` established and this keeps:

        builder.add_node(
            "implementation",
            ACPNode(agent="codex", prompt="...", cwd=checkout),
        )

    What it adds over the minimal node is everything the control surface needs:
    the session becomes the execution's, its updates become runtime events, its
    permission requests become approvals answerable by a different process, and
    steering sent while it works becomes another turn in the same conversation.
    """

    agent: str
    """The provider name to resolve: `"codex"`, `"claude"`, an application's own."""
    prompt: str | Callable[[Mapping[str, object]], ACPPrompt] = ""
    """What to say, or how to build it from the graph's state."""
    registry: ACPAgentRegistry | None = None
    """Where `agent` resolves. The shared default when omitted."""
    session_key: str = ""
    """Which conversation within the run. The node's own id when empty."""
    output_key: str = ""
    """The state key the agent's message is written to. The node id when empty."""
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION
    """What its permission requests are reported as."""
    continuation_prompt: str = (
        "The request you were waiting on has been answered. Carry on with what "
        "you were doing; this is the same task, not a new one."
    )
    """What a resumed session is told. Never the original prompt.

    Sending that again would be a second, independent task: the agent would
    start over from the words it began with, having discarded everything it had
    worked out since. All it needs to know is that the question was answered.
    """
    graph_node_name: str = ""
    """What to call this node on screen. The node's own id when empty."""
    graph_node_kind: str = "agent"
    graph_node_description: str = ""
    cwd: str | Callable[[Mapping[str, object]], str | None]
    """Where the session works, or how to read it off the graph's state.

    Required, and with no default, which is the one field on this node worth
    arguing about. The only default available is "wherever this process happens
    to be", and that is the server's own checkout -- see
    `NoWorkingDirectoryError`. Having none is not a state this node can be in,
    so it is not expressible: omitting it fails at the `add_node` line, under a
    type checker as well as at runtime.

    A resolver rather than only a string because the directory is usually the
    run's own: one graph serves every run, and each of them is given a checkout
    of its own by whichever node provisioned it. `components.checkout` is that
    resolver for a graph with a `WorkspaceNode` in it. Resolved per invocation,
    so the node stays a description of the work rather than a copy per checkout.
    """
    mcp_servers: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # A literal is checkable now; a resolver is not, and is checked on the
        # invocation that runs it. Empty rather than absent because `cwd=""` is
        # the same accident spelled differently: ACP would resolve it against
        # the process too.
        if not callable(self.cwd) and not str(self.cwd).strip():
            raise NoWorkingDirectoryError(
                f"ACPNode for agent {self.agent!r} was given an empty working "
                "directory. Name the directory the agent may work in, or pass a "
                "resolver that reads it off the run's state."
            )

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        runtime = execution.runtime
        # First, before the store is read and before anything is launched: a
        # node that does not know where to work has nothing to resume into and
        # no session worth opening, so it fails having done nothing.
        cwd = self._cwd(state)
        key = self.session_key or str(execution.node_id)
        stored = await runtime.store.session(execution.run_id, key)
        resuming = await self._answer_to_apply(runtime, stored)
        client, session = await self._open(stored if resuming else None, cwd)
        turn = _Turn(self, execution, key, session.session_id)
        if resuming is not None:
            turn.answer, turn.answered = resuming
        _TURNS[session.session_id] = turn
        execution.attach(session)
        try:
            if resuming is None:
                await runtime.store.remember_session(
                    execution.run_id, key, self._binding(execution, session, key)
                )
            await execution.emit(
                EventKind.CONVERSATION_STARTED,
                {
                    "agent": self.agent,
                    "sessionId": session.session_id,
                    "resumed": resuming is not None,
                },
            )
            asked = self.continuation_prompt if resuming else self._prompt(state)
            # Published before the turn it starts, because a transcript that
            # holds only the agent's half is not a conversation: a reader
            # opening one has to guess what was asked, and cannot tell the work
            # the node was sent to do from the work somebody steered it into.
            await execution.say(prompt_text(asked), role="user")
            said = await self._speak(turn, session, asked)
            # Steering that arrived while the agent worked is a further turn in
            # the same conversation rather than a restart: same session id, same
            # transcript, same tool history.
            for message in execution.pending_messages():
                await execution.say(message, role="user")
                said = await self._speak(turn, session, message)
            return {self.output_key or str(execution.node_id): said}
        finally:
            _TURNS.pop(session.session_id, None)
            await client.close()

    def _binding(
        self, execution: NodeExecution, session: ACPSession, key: str
    ) -> ACPContinuation:
        return ACPContinuation(
            agent=self.agent,
            session_id=session.session_id,
            thread_id=str(execution.run_id),
            session_key=key,
        )

    async def _open(
        self, stored: ACPContinuation | None, cwd: str
    ) -> tuple[Any, ACPSession]:
        """Reach the conversation: the stored one when resuming, else a new one.

        `resume_continuation` is `langgraph-acp`'s, deliberately. What a session
        id means and how a connection reloads one is that package's to know; all
        this node does is keep the record and hand it back.
        """
        if stored is not None:
            return await resume_continuation(stored, registry=self.registry, cwd=cwd)
        provider = (self.registry or default_registry()).resolve(self.agent)
        client = await provider.connect()
        try:
            return client, await client.new_session(cwd=cwd)
        except BaseException:
            await client.close()
            raise

    def _cwd(self, state: Mapping[str, object]) -> str:
        """Where this invocation works, refusing rather than falling back.

        The resolver is the interesting case and the reachable one: a graph
        whose `WorkspaceNode` was omitted or ordered after this node, a provider
        that answered with an empty path, or a fork re-entering this node from a
        checkpoint taken before anything had been provisioned. Each of those
        leaves the state key missing, and each of them used to mean the session
        opened in the server's own tree.
        """
        resolved = self.cwd(state) if callable(self.cwd) else self.cwd
        if not resolved or not str(resolved).strip():
            raise NoWorkingDirectoryError(
                f"ACPNode for agent {self.agent!r} resolved no working directory "
                "from this run's state. A node that provisions one -- "
                "`WorkspaceNode` -- has to run before it."
            )
        return str(resolved)

    async def _answer_to_apply(
        self, runtime: Any, stored: ACPContinuation | None
    ) -> tuple[ApprovalDecision, ApprovalId] | None:
        """The answer this node was re-entered to deliver, if it was.

        Read from the store rather than from graph state: a decision made after
        the process died was never part of any superstep, so the values a
        checkpoint holds cannot know about it.
        """
        if stored is None:
            return None
        named = stored.metadata.get(APPROVAL_ID)
        if not isinstance(named, str) or not named:
            return None
        approval_id = ApprovalId(named)
        decision = await runtime.recorded_decision(approval_id)
        return None if decision is None else (decision, approval_id)

    async def _speak(self, turn: _Turn, session: ACPSession, prompt: ACPPrompt) -> str:
        """One ACP turn, with what happens in it republished as runtime events.

        Message deltas are gathered rather than published one by one -- a
        transcript event per token would be unreadable -- but they are gathered
        only as far as the next thing the agent does. An agent narrates what it
        is about to do and then does it, so a line written before a call is
        published before that call, and somebody following the run reads the
        explanation with the work it explains rather than after all of it.

        A permission request counts as something it does, and is the case that
        matters most: it is the one point where a turn can stop for as long as
        a person takes to answer. Held to the end of the turn, the sentence
        saying *why* the agent is asking would be published only once somebody
        had already answered -- so for the whole time the run was genuinely
        waiting on them, the conversation would have nothing in it at all.
        `langgraph-acp` streams the request before it calls the handler, for
        this reason, and `_Turn.narrating` is the other half of it.
        """
        execution = turn.execution
        said: list[str] = []
        pending: list[str] = []

        async def flush() -> None:
            # Emptied before anything is awaited, so the two tasks that can
            # call this cannot publish the same words twice.
            text = "".join(pending)
            pending.clear()
            if text:
                said.append(text)
                await execution.say(text)

        turn.narrating = flush
        try:
            async for event in session.prompt(prompt):
                if event.type in _INTERRUPTS_THE_NARRATION:
                    await flush()
                await self._republish(execution, event)
                if event.type == ACPEventType.MESSAGE_DELTA:
                    block = event.data.get("content")
                    if isinstance(block, Mapping) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            pending.append(text)
            await flush()
        finally:
            turn.narrating = None
        # The node's durable output is still the whole turn: what the graph
        # carries forward does not change with where the words were published.
        return "".join(said)

    async def _republish(self, execution: NodeExecution, event: Any) -> None:
        if event.type == ACPEventType.TOOL_STARTED:
            await execution.emit(
                EventKind.TOOL_CALL,
                {
                    "callId": str(event.data.get("toolCallId", "")),
                    "name": str(event.data.get("title") or event.data.get("kind") or ""),
                    "arguments": dict(event.data),
                },
            )
        elif event.type == ACPEventType.TOOL_UPDATED:
            await execution.emit(
                EventKind.TOOL_RESULT,
                {
                    "callId": str(event.data.get("toolCallId", "")),
                    "name": str(event.data.get("title") or ""),
                    "result": str(event.data.get("status") or "updated"),
                },
            )

    def _prompt(self, state: Mapping[str, object]) -> ACPPrompt:
        if callable(self.prompt):
            return self.prompt(state)
        return self.prompt

    # --- how one agent's permission request reads as an approval -----------

    def reason_for(self, request: ACPPermissionRequest) -> str:
        title = request.tool_call.get("title")
        return str(title) if isinstance(title, str) and title else "run a tool"

    def command_of(self, request: ACPPermissionRequest) -> str:
        for name in ("rawInput", "input"):
            nested = request.tool_call.get(name)
            if isinstance(nested, Mapping):
                command = nested.get("command")
                if isinstance(command, str):
                    return command
        command = request.tool_call.get("command")
        return command if isinstance(command, str) else ""

    def tool_of(self, request: ACPPermissionRequest) -> str:
        for name in ("kind", "toolCallId"):
            value = request.tool_call.get(name)
            if isinstance(value, str) and value:
                return value
        return ""

    def call_of(self, request: ACPPermissionRequest) -> str:
        """The call this request is about, in the agent's own ids.

        The same id the agent puts on the `tool_call` update it sends for the
        work itself, which is what lets a reader be shown the question beside
        the command rather than beside the turn. Empty when the agent named no
        call, and a client that gets nothing here has nothing to pair.
        """
        value = request.tool_call.get("toolCallId")
        return value if isinstance(value, str) else ""


__all__ = [
    "APPROVAL_ID",
    "EXECUTION_ID",
    "NODE_ID",
    "RUN_ID",
    "ACPNode",
    "answer_permission",
    "prompt_text",
]
