"""Agent identity, conversations, and the turn-shaped runner port.

Three levels -- role, instance, run -- and a runner that serves chat and
headless coding with the same call. These tests pin the distinctions that make
that possible, because collapsing any two of them is the easy mistake.
"""

import asyncio
from collections.abc import Sequence

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentInstance,
    AgentInstanceId,
    AgentProfile,
    AgentRun,
    AgentRunId,
    AgentRunStatus,
    Conversation,
    ConversationId,
    Message,
    Role,
    RunId,
    StartAgentRun,
    StepId,
    StepSpec,
    ToolCall,
    ToolParameter,
    ToolSpec,
)
from engine.ports import AgentRunner, AgentTurn, FinishReason
from engine.runtime import Capabilities, Dispatcher
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR

FOREMAN = AgentProfile(
    agent_id=AgentId("foreman"),
    instructions="Coordinate implementation work, answer coder questions, escalate when necessary.",
    capabilities=("dispatch", "author_workflow"),
    description="Coordinates the work.",
)


# --- the three levels are distinct -----------------------------------------


def test_profile_is_configuration_not_state() -> None:
    """A profile says what an agent is, never what it is currently doing."""
    fields = set(AgentProfile.__dataclass_fields__)
    assert fields == {
        "agent_id",
        "instructions",
        "capabilities",
        "model",
        "description",
        "read_only",
    }


def test_an_instance_owns_a_conversation_and_may_outlive_many_runs() -> None:
    instance = AgentInstance(
        instance_id=AgentInstanceId("agi-1"),
        agent_id=FOREMAN.agent_id,
        conversation_id=ConversationId("conv-1"),
    )
    runs = [
        AgentRun(agent_run_id=AgentRunId(f"ar-{n}"), instance_id=instance.instance_id)
        for n in (1, 2, 3)
    ]

    assert {run.instance_id for run in runs} == {instance.instance_id}
    assert instance.task_id is None and instance.workspace_id is None, (
        "a chat instance has no task and no checkout"
    )


def test_run_status_terminality() -> None:
    def run(status: AgentRunStatus) -> AgentRun:
        return AgentRun(AgentRunId("ar-1"), AgentInstanceId("agi-1"), status=status)

    assert not run(AgentRunStatus.PENDING).is_terminal
    assert not run(AgentRunStatus.RUNNING).is_terminal
    assert run(AgentRunStatus.SUCCEEDED).is_terminal
    assert run(AgentRunStatus.FAILED).is_terminal
    assert run(AgentRunStatus.CANCELLED).is_terminal


# --- conversations ----------------------------------------------------------


def test_appending_leaves_the_original_alone() -> None:
    """History handed to a runner cannot be edited underneath the caller."""
    conversation = Conversation(ConversationId("conv-1"), AgentInstanceId("agi-1"))

    extended = conversation.appending(Message.user("hello"))

    assert conversation.messages == ()
    assert extended.messages == (Message.user("hello"),)
    assert extended.last == Message.user("hello")


def test_tool_result_addresses_the_call_that_asked_for_it() -> None:
    call = ToolCall(call_id="call-1", name="dispatch", arguments='{"task": "ENG-42"}')
    result = Message.tool_result(call.call_id, "dispatched run-7")

    assert result.role is Role.TOOL
    assert result.tool_call_id == call.call_id


def test_tool_arguments_stay_unparsed() -> None:
    """Malformed arguments must survive intact -- they get handed back to the
    model as a tool error, which is impossible if the domain rejected them."""
    call = ToolCall(call_id="call-1", name="dispatch", arguments="{not json")

    assert call.arguments == "{not json"


# --- the runner port --------------------------------------------------------


class FakeAgentRunner:
    """Satisfies `AgentRunner` by shape alone. Replies with a canned turn, and
    records what it was asked, so a test can assert on the whole call."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, turn: AgentTurn | None = None) -> None:
        self.turn = turn or AgentTurn(Message.assistant("on it"))
        self.calls: list[tuple[AgentRunId, AgentProfile, tuple[Message, ...], tuple[ToolSpec, ...]]] = []
        self.cancelled: list[AgentRunId] = []

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: str | None = None,
    ) -> AgentTurn:
        self.calls.append((agent_run_id, profile, tuple(messages), tuple(tools)))
        return self.turn

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        self.cancelled.append(agent_run_id)


def test_fake_satisfies_the_port_structurally() -> None:
    assert isinstance(FakeAgentRunner(), AgentRunner)


def test_a_plain_answer_asks_for_no_tools() -> None:
    turn = AgentTurn(Message.assistant("here is the plan"))

    assert not turn.wants_tools
    assert turn.tool_calls == ()
    assert turn.finish_reason is FinishReason.STOP


def test_a_turn_carries_its_tool_calls_on_the_message() -> None:
    """On the message, not beside it: appending the turn to a conversation has
    to preserve the request, or the tool results reference nothing."""
    call = ToolCall(call_id="call-1", name="dispatch", arguments="{}")
    turn = AgentTurn(
        Message.assistant(tool_calls=(call,)), finish_reason=FinishReason.TOOL_CALLS
    )

    conversation = Conversation(ConversationId("c"), AgentInstanceId("i")).appending(
        turn.message, Message.tool_result(call.call_id, "run-7")
    )

    assert turn.wants_tools and turn.tool_calls == (call,)
    assert conversation.messages[0].tool_calls == (call,)
    assert conversation.messages[1].tool_call_id == call.call_id


def test_the_same_runner_serves_chat_and_coding() -> None:
    """The architectural claim: a workspace is optional context, not a mode."""
    runner = FakeAgentRunner()

    asyncio.run(
        runner.run_turn(AgentRunId("ar-1"), FOREMAN, (Message.user("status?"),))
    )
    asyncio.run(
        runner.run_turn(
            AgentRunId("ar-2"),
            FOREMAN,
            (Message.user("fix the flaky test"),),
            workspace_id="ws-1",
        )
    )

    assert [call[0] for call in runner.calls] == [AgentRunId("ar-1"), AgentRunId("ar-2")]


def test_tools_offered_are_the_caller_s_to_choose() -> None:
    """A runner is handed resolved specs; it never reads the grants itself, so
    an adapter cannot widen what a profile permits."""
    runner = FakeAgentRunner()
    dispatch = ToolSpec(
        name="dispatch",
        description="Start a run.",
        parameters=(ToolParameter(name="task_id", description="Task to run."),),
    )

    asyncio.run(
        runner.run_turn(AgentRunId("ar-1"), FOREMAN, (Message.user("go"),), tools=(dispatch,))
    )

    assert runner.calls[0][3] == (dispatch,)
    assert dispatch.required_parameters == ("task_id",)


# --- dispatch ---------------------------------------------------------------


def _capabilities(agent_runner: object) -> Capabilities:
    missing = object()  # unused capabilities are never touched by this command
    return Capabilities(
        workflow_runtime=missing,
        source_control=missing,
        agent_runner=agent_runner,
        communications=missing,
        workspace_provider=missing,
        state_store=missing,
    )


def test_dispatch_routes_a_start_to_the_agent_runner() -> None:
    runner = FakeAgentRunner()
    command = StartAgentRun(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("ar-1"),
        instance_id=AgentInstanceId("agi-1"),
        profile=FOREMAN,
        prompt="status?",
    )

    asyncio.run(Dispatcher(_capabilities(runner)).dispatch(command))

    agent_run_id, profile, messages, _ = runner.calls[0]
    assert agent_run_id == AgentRunId("ar-1")
    assert profile is FOREMAN
    assert messages == (Message.user("status?"),)


def test_the_command_carries_the_profile_so_dispatch_needs_no_registry() -> None:
    """Self-contained by design: a durable replay reruns the agent as it was
    configured when the run started."""
    command = StartAgentRun(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("ar-1"),
        instance_id=AgentInstanceId("agi-1"),
        profile=FOREMAN,
        prompt="go",
    )

    assert command.profile.instructions == FOREMAN.instructions
    assert command.workspace_id is None


def test_workflow_start_materializes_explicit_step_conversation_correlation() -> None:
    runner = FakeAgentRunner()
    store = InMemoryStateStore()
    missing = object()
    capabilities = Capabilities(
        workflow_runtime=missing,
        source_control=missing,
        agent_runner=runner,
        communications=missing,
        workspace_provider=missing,
        state_store=store,
    )
    command = StartAgentRun(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("implementation-execution"),
        instance_id=AgentInstanceId("implementation-instance"),
        profile=FOREMAN,
        prompt="Implement the task.",
        step=StepSpec(StepId("implementation"), FOREMAN.agent_id),
    )

    asyncio.run(
        Dispatcher(capabilities).run_workflow_agent(
            command, runner_name="codex"
        )
    )

    instance = asyncio.run(store.load_instance(command.instance_id))
    conversation = asyncio.run(store.load_conversation(command.instance_id))
    agent_run = asyncio.run(store.agent_run(command.agent_run_id))
    assert instance is not None
    assert instance.workflow_run_id == command.run_id
    assert instance.workflow_step_id == command.step.step_id
    assert instance.conversation_id == "implementation-instance:conversation"
    assert instance.runner == "codex"
    assert conversation is not None
    assert conversation.messages[0].role is Role.USER
    assert conversation.messages[0].content.startswith("Implement the task.")
    assert "`complete_step`" in conversation.messages[0].content
    assert "JSON" not in conversation.messages[0].content
    assert conversation.messages[1].role is Role.ASSISTANT
    assert conversation.messages[1].content == "on it"
    assert agent_run is not None
    assert agent_run.instance_id == command.instance_id
    assert agent_run.status is AgentRunStatus.SUCCEEDED
    assert agent_run.runner == "codex"


@pytest.mark.parametrize(
    "method",
    [
        "create_instance",
        "update_instance_metadata",
        "load_instance",
        "list_instances",
        "load_conversation",
        "append_messages",
        "record_agent_run",
    ],
)
def test_state_store_owns_the_conversation(method: str) -> None:
    """Not the model provider: history has to outlive a vendor's session."""
    from engine.ports import StateStore

    assert hasattr(StateStore, method)
