"""Conversations: the store that holds them and the session that drives them.

`AgentSession` is the seam every chat surface goes through, so what it does
around a turn -- ordering, persistence, refusing what it cannot honour -- is
tested here rather than in whichever interface happens to call it.
"""

import asyncio
from collections.abc import Sequence

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    Message,
    Role,
    ToolCall,
    ToolSpec,
)
from engine.ports import (
    AgentTurn,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    StateStore,
)
from engine.runtime import (
    DEFAULT_RUNNER,
    INTERRUPTED_TOOL_RESULT,
    INTERRUPTED_TURN_NOTE,
    AgentSession,
    Capabilities,
    UnknownAgentError,
    UnknownInstanceError,
    UnknownRunnerError,
    UnknownToolGrantError,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(agent_id=CODER, instructions="Be terse.", description="Reads code."),
}


class ScriptedRunner:
    """Answers from a list. Records every call so ordering can be asserted."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self._replies = list(replies)
        self.seen: list[tuple[AgentRunId, AgentProfile, tuple[Message, ...]]] = []

    async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
        self.seen.append((agent_run_id, profile, tuple(messages)))
        reply = self._replies.pop(0) if self._replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id) -> None:
        pass


class InterruptedRunner(ScriptedRunner):
    """Reports work, asks for one more tool, and never comes back.

    The shape of the moment somebody presses stop: some of what the agent did
    has been reported, and the last thing it did is a call whose result was
    still on its way.
    """

    READ = ToolCall(call_id="c1", name="Read", arguments='{"path": "worker.py"}')
    WRITE = ToolCall(call_id="c2", name="Write", arguments='{"path": "worker.py"}')

    def __init__(self) -> None:
        super().__init__()
        self.reported = asyncio.Event()

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("the streaming method should be used")

    async def run_turn_streamed(
        self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
    ):
        self.seen.append((agent_run_id, profile, tuple(messages)))
        on_message(Message.assistant("Reading the worker first."))
        on_message(Message.assistant(tool_calls=(self.READ,)))
        on_message(Message.tool_result("c1", "def work(): ..."))
        on_message(Message.assistant(tool_calls=(self.WRITE,)))
        self.reported.set()
        await asyncio.Event().wait()
        raise AssertionError("this runner only ever ends by being cancelled")


async def _stopped_mid_turn(
    session: AgentSession, runner: InterruptedRunner, instance_id
) -> None:
    """Start a turn, let it report what it has done, then stop it."""
    turn = asyncio.create_task(
        session.say(instance_id, "fix the worker", on_message=lambda _message: None)
    )
    await runner.reported.wait()
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)


class BrokenRunner:
    """Fails the way a real one does: after the question was asked."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self) -> None:
        self.seen: list[AgentRunId] = []

    async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
        self.seen.append(agent_run_id)
        raise RuntimeError("codex exited 1")

    async def cancel(self, agent_run_id) -> None:
        pass


def _session(runner: object, store: InMemoryStateStore) -> AgentSession:
    missing = object()
    capabilities = Capabilities(
        workflow_runtime=missing,
        source_control=missing,
        agent_runner=runner,
        communications=missing,
        workspace_provider=missing,
        state_store=store,
    )
    return AgentSession(capabilities, profiles=PROFILES)


# --- the store ---------------------------------------------------------------


def test_memory_store_satisfies_the_port() -> None:
    assert isinstance(InMemoryStateStore(), StateStore)


def test_an_instance_is_born_with_its_conversation() -> None:
    """Never one without the other -- a half-created instance is a state no
    reader should have to handle."""
    store = InMemoryStateStore()

    instance = asyncio.run(store.create_instance(CODER))
    conversation = asyncio.run(store.load_conversation(instance.instance_id))

    assert conversation is not None
    assert conversation.conversation_id == instance.conversation_id
    assert conversation.messages == ()


def test_instance_metadata_is_updated_together() -> None:
    store = InMemoryStateStore()
    instance = asyncio.run(store.create_instance(CODER, runner="codex"))

    updated = asyncio.run(
        store.update_instance_metadata(
            instance.instance_id, "Named chat", True, "claude", True
        )
    )

    assert updated.title == "Named chat"
    assert updated.archived is True
    assert updated.runner == "claude"
    assert updated.auto_approve is True
    assert asyncio.run(store.load_instance(instance.instance_id)) == updated


def test_messages_keep_their_order_and_get_ids() -> None:
    store = InMemoryStateStore()
    instance = asyncio.run(store.create_instance(CODER))

    asyncio.run(store.append_messages(instance.instance_id, (Message.user("one"),)))
    asyncio.run(
        store.append_messages(
            instance.instance_id, (Message.assistant("two"), Message.user("three"))
        )
    )
    messages = asyncio.run(store.load_conversation(instance.instance_id)).messages

    assert [m.content for m in messages] == ["one", "two", "three"]
    assert all(m.message_id for m in messages)
    assert len({m.message_id for m in messages}) == 3


def test_writing_to_an_unknown_instance_is_refused() -> None:
    with pytest.raises(KeyError):
        asyncio.run(InMemoryStateStore().append_messages("agi-nope", (Message.user("x"),)))


def test_instances_list_newest_first_and_filter_by_role() -> None:
    store = InMemoryStateStore()
    first = asyncio.run(store.create_instance(CODER))
    second = asyncio.run(store.create_instance(CODER))
    other = asyncio.run(store.create_instance(AgentId("foreman")))

    everything = asyncio.run(store.list_instances())
    coders = asyncio.run(store.list_instances(CODER))

    assert [i.instance_id for i in everything][0] == other.instance_id
    assert [i.instance_id for i in coders] == [second.instance_id, first.instance_id]


def test_two_stores_do_not_share_state() -> None:
    """Module-level dicts would make every test and every process the same
    conversation."""
    first, second = InMemoryStateStore(), InMemoryStateStore()
    asyncio.run(first.create_instance(CODER))

    assert asyncio.run(second.list_instances()) == ()


# --- the session -------------------------------------------------------------


def test_a_turn_stores_both_sides() -> None:
    store = InMemoryStateStore()
    session = _session(ScriptedRunner(["4"]), store)
    instance = asyncio.run(session.start(CODER))

    turn = asyncio.run(session.say(instance.instance_id, "what is 2+2"))
    history = asyncio.run(session.history(instance.instance_id))

    assert turn.message.content == "4"
    assert [(m.role, m.content) for m in history] == [
        (Role.USER, "what is 2+2"),
        (Role.ASSISTANT, "4"),
    ]


def test_a_non_streaming_runner_still_reports_the_completed_transcript() -> None:
    store = InMemoryStateStore()
    session = _session(ScriptedRunner(["4"]), store)
    instance = asyncio.run(session.start(CODER))
    observed: list[Message] = []

    turn = asyncio.run(
        session.say(instance.instance_id, "what is 2+2", on_message=observed.append)
    )

    assert observed == list(turn.transcript)


def test_a_streaming_runner_reports_steps_before_the_final_answer() -> None:
    store = InMemoryStateStore()
    call = ToolCall(call_id="c1", name="Read", arguments='{"path": "README.md"}')
    steps = (
        Message.assistant("I will look."),
        Message.assistant(tool_calls=(call,)),
        Message.tool_result("c1", "engine"),
    )

    class StreamingRunner(ScriptedRunner):
        async def run_turn(self, *args, **kwargs):
            raise AssertionError("the streaming method should be used")

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ):
            turn = AgentTurn(Message.assistant("Done."), steps=steps)
            for message in turn.transcript:
                on_message(message)
            return turn

    session = _session(StreamingRunner(), store)
    instance = asyncio.run(session.start(CODER))
    observed: list[Message] = []

    turn = asyncio.run(session.say(instance.instance_id, "inspect", on_message=observed.append))

    assert observed == list(turn.transcript)
    stored = asyncio.run(session.history(instance.instance_id))[-4:]
    assert [(message.role, message.content) for message in stored] == [
        (message.role, message.content) for message in turn.transcript
    ]


def test_an_interactive_runner_can_await_an_approval_decision() -> None:
    store = InMemoryStateStore()
    request = ApprovalRequest(
        approval_id="approval-1",
        kind=ApprovalKind.COMMAND_EXECUTION,
        reason="This command writes a generated file.",
        command="make generate",
        cwd="/workspace",
    )

    class InteractiveRunner(ScriptedRunner):
        def __init__(self) -> None:
            super().__init__()
            self.decision: ApprovalDecision | None = None

        async def run_turn(self, *args, **kwargs):
            raise AssertionError("the interactive method should be used")

        async def run_turn_interactive(
            self,
            agent_run_id,
            profile,
            messages,
            on_approval,
            on_message=None,
            tools=(),
            workspace_id=None,
        ):
            self.decision = await on_approval(request)
            turn = AgentTurn(Message.assistant("Generated."))
            if on_message is not None:
                on_message(turn.message)
            return turn

    runner = InteractiveRunner()
    session = _session(runner, store)
    instance = asyncio.run(session.start(CODER))
    presented: list[ApprovalRequest] = []
    observed: list[Message] = []

    async def approve(pending: ApprovalRequest) -> ApprovalDecision:
        presented.append(pending)
        return ApprovalDecision.ACCEPT

    turn = asyncio.run(
        session.say(
            instance.instance_id,
            "generate the client",
            on_message=observed.append,
            on_approval=approve,
        )
    )

    assert presented == [request]
    assert runner.decision is ApprovalDecision.ACCEPT
    assert observed == [turn.message]
    stored = asyncio.run(session.history(instance.instance_id))[-1]
    assert (stored.role, stored.content) == (turn.message.role, turn.message.content)


def test_what_the_agent_did_is_stored_alongside_what_it_said() -> None:
    """An agent that read a file before answering leaves the read in the
    transcript. Without it, nobody can later tell why the answer is what it is."""
    store = InMemoryStateStore()
    call = ToolCall(call_id="c1", name="command_execution", arguments='{"command": "ls"}')
    steps = (Message.assistant(tool_calls=(call,)), Message.tool_result("c1", "README.md"))

    class StepRunner:
        permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

        async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
            return AgentTurn(Message.assistant("one file"), steps=steps)

        async def cancel(self, agent_run_id) -> None:
            pass

    session = _session(StepRunner(), store)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "what is in there?"))
    history = asyncio.run(session.history(instance.instance_id))

    assert [(m.role, m.content) for m in history] == [
        (Role.USER, "what is in there?"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "README.md"),
        (Role.ASSISTANT, "one file"),
    ]
    assert history[1].tool_calls == (call,)
    assert history[2].tool_call_id == "c1"


def test_recorded_steps_go_back_to_the_agent_next_turn() -> None:
    """A stateless runner starts cold, so the steps are how it remembers what it
    already did."""
    store = InMemoryStateStore()
    runner = ScriptedRunner(["first", "second"])

    class OneStepRunner(ScriptedRunner):
        async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
            turn = await super().run_turn(agent_run_id, profile, messages)
            return AgentTurn(turn.message, steps=(Message.assistant("(looked something up)"),))

    runner = OneStepRunner(["first", "second"])
    session = _session(runner, store)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "one"))
    asyncio.run(session.say(instance.instance_id, "two"))

    _, _, second_call = runner.seen[1]
    assert [m.content for m in second_call] == [
        "one",
        "(looked something up)",
        "first",
        "two",
    ]


def test_the_agent_is_given_the_whole_conversation() -> None:
    """Including the message it is answering -- the runner loads no history of
    its own, so anything missing here is missing from the model's context."""
    store = InMemoryStateStore()
    runner = ScriptedRunner(["4", "12"])
    session = _session(runner, store)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "what is 2+2"))
    asyncio.run(session.say(instance.instance_id, "and times 3"))

    _, profile, second_call = runner.seen[1]
    assert profile.agent_id == CODER
    assert [m.content for m in second_call] == ["what is 2+2", "4", "and times 3"]


def test_each_turn_is_its_own_run() -> None:
    store = InMemoryStateStore()
    runner = ScriptedRunner(["a", "b"])
    session = _session(runner, store)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "one"))
    asyncio.run(session.say(instance.instance_id, "two"))

    run_ids = [call[0] for call in runner.seen]
    assert len(set(run_ids)) == 2
    assert all(asyncio.run(store.agent_run(rid)).status is AgentRunStatus.SUCCEEDED for rid in run_ids)


def test_a_failed_turn_leaves_the_question_in_the_transcript() -> None:
    """An accurate record of a question with no answer beats losing what was
    asked."""
    store = InMemoryStateStore()
    session = _session(BrokenRunner(), store)
    instance = asyncio.run(session.start(CODER))

    with pytest.raises(RuntimeError):
        asyncio.run(session.say(instance.instance_id, "will this work"))

    history = asyncio.run(session.history(instance.instance_id))
    assert [(m.role, m.content) for m in history] == [(Role.USER, "will this work")]


def test_a_failed_turn_is_recorded_as_failed() -> None:
    store = InMemoryStateStore()
    runner = BrokenRunner()
    session = _session(runner, store)
    instance = asyncio.run(session.start(CODER))

    with pytest.raises(RuntimeError):
        asyncio.run(session.say(instance.instance_id, "go"))

    run = asyncio.run(store.agent_run(runner.seen[0]))
    assert run.status is AgentRunStatus.FAILED
    assert "codex exited 1" in run.summary


def test_a_stopped_turn_keeps_the_work_it_had_already_reported() -> None:
    """Stopping an agent does not undo what it did: the file it wrote is still
    written. A transcript that leaves the work out is one the workspace
    contradicts -- and the next turn believes the transcript."""
    store = InMemoryStateStore()
    runner = InterruptedRunner()
    session = _session(runner, store)

    async def scenario():
        instance = await session.start(CODER)
        await _stopped_mid_turn(session, runner, instance.instance_id)
        return (
            await session.history(instance.instance_id),
            await store.agent_run(runner.seen[0][0]),
        )

    history, agent_run = asyncio.run(scenario())

    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "fix the worker"),
        (Role.ASSISTANT, "Reading the worker first."),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "def work(): ..."),
        (Role.ASSISTANT, ""),
        # The call that was still in flight is answered, so it cannot be read
        # later as a tool that ran and returned nothing.
        (Role.TOOL, INTERRUPTED_TOOL_RESULT),
        (Role.SYSTEM, INTERRUPTED_TURN_NOTE),
    ]
    assert history[4].tool_calls == (InterruptedRunner.WRITE,)
    assert history[5].tool_call_id == "c2"
    assert agent_run.status is AgentRunStatus.CANCELLED


def test_the_next_turn_is_told_what_the_stopped_one_did() -> None:
    """Storing it is only half of it. A runner starts cold every time, so the
    stored partial is the only way the turn after an interruption knows any of
    it happened."""
    store = InMemoryStateStore()
    stopped = InterruptedRunner()
    resumed = ScriptedRunner(["Picked up where it left off."])

    async def scenario():
        session = _session(stopped, store)
        instance = await session.start(CODER)
        await _stopped_mid_turn(session, stopped, instance.instance_id)
        await _session(resumed, store).say(instance.instance_id, "carry on")
        return resumed.seen[0][2]

    given = asyncio.run(scenario())

    assert [message.content for message in given] == [
        "fix the worker",
        "Reading the worker first.",
        "",
        "def work(): ...",
        "",
        INTERRUPTED_TOOL_RESULT,
        INTERRUPTED_TURN_NOTE,
        "carry on",
    ]


def test_conversations_do_not_bleed_into_each_other() -> None:
    store = InMemoryStateStore()
    session = _session(ScriptedRunner(["a", "b"]), store)
    first = asyncio.run(session.start(CODER))
    second = asyncio.run(session.start(CODER))

    asyncio.run(session.say(first.instance_id, "to the first"))
    asyncio.run(session.say(second.instance_id, "to the second"))

    assert [m.content for m in asyncio.run(session.history(first.instance_id))] == [
        "to the first",
        "a",
    ]
    assert [m.content for m in asyncio.run(session.history(second.instance_id))] == [
        "to the second",
        "b",
    ]


def test_an_unknown_agent_fails_before_anything_is_stored() -> None:
    store = InMemoryStateStore()
    session = _session(ScriptedRunner(), store)

    with pytest.raises(UnknownAgentError):
        asyncio.run(session.start(AgentId("nobody")))

    assert asyncio.run(store.list_instances()) == ()


def test_an_unknown_instance_is_a_clear_error() -> None:
    session = _session(ScriptedRunner(), InMemoryStateStore())

    with pytest.raises(UnknownInstanceError):
        asyncio.run(session.say("agi-nope", "hello"))


def test_a_grant_that_resolves_to_nothing_stops_the_turn() -> None:
    """A foreman that cannot dispatch is not a foreman; finding that out from
    its answers rather than an exception wastes everybody's time."""
    store = InMemoryStateStore()
    runner = ScriptedRunner()
    foreman = AgentId("foreman")
    session = AgentSession(
        Capabilities(
            workflow_runtime=None,
            source_control=None,
            agent_runner=runner,
            communications=None,
            workspace_provider=None,
            state_store=store,
        ),
        profiles={
            foreman: AgentProfile(
                agent_id=foreman, instructions="Coordinate.", capabilities=("dispatch",)
            )
        },
    )
    instance = asyncio.run(session.start(foreman))

    with pytest.raises(UnknownToolGrantError) as raised:
        asyncio.run(session.say(instance.instance_id, "dispatch ENG-42"))

    assert raised.value.missing == ("dispatch",)
    assert runner.seen == [], "the agent must not run without the tools it was granted"


def test_a_resolvable_grant_reaches_the_runner() -> None:
    """The other half: grants are not merely rejected, they are passed through
    once something provides them."""
    store = InMemoryStateStore()
    dispatch = ToolSpec(name="dispatch", description="Start a run.")
    captured: list[tuple[ToolSpec, ...]] = []

    class ToolAwareRunner(ScriptedRunner):
        async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
            captured.append(tuple(tools))
            return await super().run_turn(agent_run_id, profile, messages)

    foreman = AgentId("foreman")
    session = AgentSession(
        Capabilities(
            workflow_runtime=None,
            source_control=None,
            agent_runner=ToolAwareRunner(),
            communications=None,
            workspace_provider=None,
            state_store=store,
        ),
        profiles={
            foreman: AgentProfile(
                agent_id=foreman, instructions="Coordinate.", capabilities=("dispatch",)
            )
        },
        tools={"dispatch": dispatch},
    )
    instance = asyncio.run(session.start(foreman))

    asyncio.run(session.say(instance.instance_id, "dispatch ENG-42"))

    assert captured == [(dispatch,)]


# --- choosing a runner -------------------------------------------------------


def test_one_wired_runner_needs_no_name() -> None:
    session = _session(ScriptedRunner(), InMemoryStateStore())

    assert session.runners == (DEFAULT_RUNNER,)
    assert session.default_runner == DEFAULT_RUNNER


def _two_runner_session(store: InMemoryStateStore, first, second) -> AgentSession:
    missing = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=missing,
            source_control=missing,
            agent_runner=first,
            communications=missing,
            workspace_provider=missing,
            state_store=store,
        ),
        profiles=PROFILES,
        runners={"first": first, "second": second},
    )


def test_the_named_runner_answers() -> None:
    store = InMemoryStateStore()
    first, second = ScriptedRunner(["from first"]), ScriptedRunner(["from second"])
    session = _two_runner_session(store, first, second)
    instance = asyncio.run(session.start(CODER))

    turn = asyncio.run(session.say(instance.instance_id, "hello", runner="second"))

    assert turn.message.content == "from second"
    assert first.seen == []


def test_the_first_wired_runner_is_the_default() -> None:
    store = InMemoryStateStore()
    first, second = ScriptedRunner(["from first"]), ScriptedRunner(["from second"])
    session = _two_runner_session(store, first, second)
    instance = asyncio.run(session.start(CODER))

    turn = asyncio.run(session.say(instance.instance_id, "hello"))

    assert turn.message.content == "from first"
    assert session.runners == ("first", "second")


def test_one_conversation_may_be_continued_by_either_runner() -> None:
    """The payoff of holding the transcript ourselves: whoever answers next is
    handed everything the other one said."""
    store = InMemoryStateStore()
    first, second = ScriptedRunner(["from first"]), ScriptedRunner(["from second"])
    session = _two_runner_session(store, first, second)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "one", runner="first"))
    asyncio.run(session.say(instance.instance_id, "two", runner="second"))

    _, _, seen_by_second = second.seen[0]
    assert [m.content for m in seen_by_second] == ["one", "from first", "two"]


def test_which_runner_answered_is_recorded() -> None:
    store = InMemoryStateStore()
    first, second = ScriptedRunner(["a"]), ScriptedRunner(["b"])
    session = _two_runner_session(store, first, second)
    instance = asyncio.run(session.start(CODER))

    asyncio.run(session.say(instance.instance_id, "hello", runner="second"))

    agent_run_id = second.seen[0][0]
    assert asyncio.run(store.agent_run(agent_run_id)).runner == "second"


def test_an_unknown_runner_stops_before_anything_is_stored() -> None:
    store = InMemoryStateStore()
    session = _session(ScriptedRunner(), store)
    instance = asyncio.run(session.start(CODER))

    with pytest.raises(UnknownRunnerError):
        asyncio.run(session.say(instance.instance_id, "hello", runner="gpt-9"))

    assert asyncio.run(session.history(instance.instance_id)) == ()


# --- the profiles that ship --------------------------------------------------


def test_shipped_profiles_grant_nothing_they_cannot_honour() -> None:
    """The foreman's dispatch grants and the project manager's workflow ones go
    in when the tools do; declaring them now would make every conversation
    raise."""
    from engine.runtime import BUILT_IN

    assert set(BUILT_IN) == {
        AgentId("foreman"),
        AgentId("coder"),
        AgentId("project-manager"),
    }
    assert all(profile.capabilities == () for profile in BUILT_IN.values())
    assert all(profile.instructions.strip() for profile in BUILT_IN.values())
