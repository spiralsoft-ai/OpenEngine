"""Talking to an agent: the mechanics around one turn.

`AgentRunner` answers a question; it does not remember, persist, or decide what
the agent is allowed to use. Those are execution mechanics, which belong to the
runtime -- so this is where loading history, appending to it, recording the run,
and resolving a profile's grants happen. A caller (a UI, an HTTP handler, a
dispatched command) gets one method: say something, get a turn back.

Keeping it here rather than in the interface means the Streamlit page and the
control server cannot drift into two subtly different notions of what a
conversation is.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from uuid import uuid4

from engine.domain.agents import AgentInstance, AgentProfile, AgentRun, AgentRunStatus
from engine.domain.chat import Message, Role
from engine.domain.ids import AgentId, AgentInstanceId, AgentRunId, TaskId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import (
    AgentRunner,
    AgentTurn,
    ApprovalHandler,
    InteractiveAgentRunner,
    StreamingAgentRunner,
    TurnObserver,
)
from engine.ports.workspace_provider import WorkspaceState
from engine.ports.state_store import StateStore
from engine.runtime.approvals import ApprovalsUnsupportedError
from engine.runtime.capabilities import Capabilities
from engine.runtime.profiles import BUILT_IN, profile_for

#: Grant name -> the tool it resolves to. Empty until tools exist; a profile
#: granting anything therefore fails loudly, which is the intended behaviour.
NO_TOOLS: Mapping[str, ToolSpec] = {}

#: What the single wired runner is called when the composition root does not
#: name several.
DEFAULT_RUNNER = "default"

#: Stands in for the result of a tool call the interruption arrived in the
#: middle of. Every call needs an answer: a request with nothing after it reads
#: to the next turn as a tool that returned nothing, rather than one that was
#: cut off with its work possibly already done.
INTERRUPTED_TOOL_RESULT = "interrupted"

#: Said once, after a stopped turn's work. The work is the part that cannot be
#: taken back -- files it changed are still changed -- so a later turn that is
#: not told this either repeats what already happened or reports as its own
#: work it never did.
INTERRUPTED_TURN_NOTE = (
    "The turn above was interrupted before it finished. Some reported actions "
    "or file changes may already have happened -- inspect the workspace rather "
    "than assuming the work was completed or undone."
)


class UnknownInstanceError(KeyError):
    """The store has never heard of that instance."""

    def __init__(self, instance_id: AgentInstanceId) -> None:
        super().__init__(f"no agent instance {instance_id!r}")
        self.instance_id = instance_id


class UnknownRunnerError(KeyError):
    """No runner is registered under that name."""

    def __init__(self, name: str, known: Sequence[str]) -> None:
        super().__init__(f"no runner {name!r}; wired: {sorted(known)}")
        self.name = name


class WorkspacesUnavailableError(RuntimeError):
    """This process was not told which repository to check workspaces out of.

    Wiring a workspace repository is the composition root's decision, so a
    conversation cannot talk itself into one.
    """

    def __init__(self) -> None:
        super().__init__("this session was composed without a workspace repository")


class UnknownToolGrantError(RuntimeError):
    """A profile grants a tool nothing can provide.

    Refusing beats running the agent anyway: a foreman that cannot dispatch is
    not a foreman, and discovering that from its answers rather than from an
    exception wastes everybody's time.
    """

    def __init__(self, agent_id: AgentId, missing: Sequence[str]) -> None:
        super().__init__(
            f"profile {agent_id!r} grants {list(missing)}, which resolve to no tool"
        )
        self.agent_id = agent_id
        self.missing = tuple(missing)


class AgentSession:
    """Conversations with agents, over a wired capability set."""

    def __init__(
        self,
        capabilities: Capabilities,
        profiles: Mapping[AgentId, AgentProfile] = BUILT_IN,
        tools: Mapping[str, ToolSpec] = NO_TOOLS,
        runners: Mapping[str, AgentRunner] | None = None,
        workspace_repository: str | None = None,
        workspace_base_ref: str = "HEAD",
        read_only_runners: Mapping[str, AgentRunner] | None = None,
    ) -> None:
        """`runners` lets one process offer a choice of agent runner.

        The names are the composition root's to invent and mean nothing here --
        the same arrangement as tool grants. Omit it and the single runner from
        `Capabilities` is used, which is what the dispatcher does for runs that
        nobody is sitting in front of.

        Switching runner mid-conversation is allowed, and is the point: we hold
        the transcript, so whichever one answers next is handed everything that
        came before it, including what the other one did.

        `read_only_runners` is the same choice of provider without the tools to
        change anything, keyed by the same names, and is what a profile that
        only reads is answered by. Composed without it, such a profile runs on
        the runner that was picked and only its instructions hold it -- so a
        process that wants the restriction wires both maps.
        """
        self._capabilities = capabilities
        self._profiles = profiles
        self._tools = tools
        self._workspace_repository = workspace_repository
        self._workspace_base_ref = workspace_base_ref
        self._runners: Mapping[str, AgentRunner] = (
            dict(runners) if runners else {DEFAULT_RUNNER: capabilities.agent_runner}
        )
        self._read_only_runners: Mapping[str, AgentRunner] = dict(read_only_runners or {})

    @property
    def profiles(self) -> Mapping[AgentId, AgentProfile]:
        return self._profiles

    @property
    def runners(self) -> tuple[str, ...]:
        """The runner names this process offers, in the order it wired them."""
        return tuple(self._runners)

    @property
    def default_runner(self) -> str:
        return next(iter(self._runners))

    @property
    def state_store(self) -> StateStore:
        """The durable boundary used by read-side services in the same process."""
        return self._capabilities.state_store

    @property
    def capabilities(self) -> Capabilities:
        """The capability set used by application-level runtime coordinators."""
        return self._capabilities

    async def start(
        self, agent_id: AgentId, task_id: TaskId | None = None, runner: str = ""
    ) -> AgentInstance:
        """Begin a conversation with an agent. Fails before touching the store
        if the profile is unknown."""
        profile_for(agent_id, self._profiles)
        if self._workspace_repository is None:
            return await self._capabilities.state_store.create_instance(
                agent_id, task_id, runner=runner
            )

        workspace = await self._capabilities.workspace_provider.provision(
            self._workspace_repository, self._workspace_base_ref
        )
        try:
            return await self._capabilities.state_store.create_instance(
                agent_id, task_id, workspace.workspace_id, runner
            )
        except Exception:
            await self._capabilities.workspace_provider.dispose(workspace.workspace_id)
            raise

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
        auto_approve: bool = False,
    ) -> AgentInstance:
        return await self._capabilities.state_store.update_instance_metadata(
            instance_id, title, archived, runner, auto_approve
        )

    async def instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        return await self._capabilities.state_store.list_instances(agent_id)

    async def instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        """Load one durable instance, including workflow ownership metadata."""
        return await self._capabilities.state_store.load_instance(instance_id)

    async def history(self, instance_id: AgentInstanceId) -> tuple[Message, ...]:
        conversation = await self._capabilities.state_store.load_conversation(instance_id)
        if conversation is None:
            raise UnknownInstanceError(instance_id)
        return conversation.messages

    async def workspace_root(self, instance_id: AgentInstanceId) -> str | None:
        """Return this conversation's checkout directory, when it has one."""
        instance = await self._capabilities.state_store.load_instance(instance_id)
        if instance is None:
            raise UnknownInstanceError(instance_id)
        if instance.workspace_id is None:
            return None
        return await self._capabilities.workspace_provider.root_path(instance.workspace_id)

    async def workspace(self, instance_id: AgentInstanceId) -> WorkspaceState | None:
        """This conversation's workspace, attached or not.

        None means the conversation has never been given one -- distinct from
        having one whose checkout is currently gone, which is a `WorkspaceState`
        with no `root_path` and is what `attach_workspace` puts back.
        """
        instance = await self._require_instance(instance_id)
        if instance.workspace_id is None:
            return None
        return await self._capabilities.workspace_provider.state(instance.workspace_id)

    async def attach_workspace(
        self,
        instance_id: AgentInstanceId,
        *,
        repository: str | None = None,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        """Give this conversation a checkout to work in, and keep the pairing.

        Works from any starting point: a conversation that never had a
        workspace is given one, and one whose checkout was detached or deleted
        gets it back, carrying in whatever work its previous checkout left
        behind.

        Workflow conversations override the session defaults with the
        repository that originally provisioned their shared workspace.
        """
        instance = await self._require_instance(instance_id)
        selected_repository = repository or self._workspace_repository
        selected_base_ref = base_ref or self._workspace_base_ref
        if selected_repository is None:
            raise WorkspacesUnavailableError()
        provider = self._capabilities.workspace_provider

        if instance.workspace_id is None:
            workspace = await provider.provision(
                selected_repository, selected_base_ref
            )
            try:
                await self._capabilities.state_store.attach_workspace(
                    instance_id, workspace.workspace_id
                )
            except Exception:
                await provider.dispose(workspace.workspace_id)
                raise
        else:
            workspace = await provider.attach(
                instance.workspace_id,
                selected_repository,
                selected_base_ref,
            )
        return WorkspaceState(
            workspace_id=workspace.workspace_id,
            ref=workspace.ref,
            root_path=workspace.root_path,
        )

    async def detach_workspace(self, instance_id: AgentInstanceId) -> WorkspaceState | None:
        """Release the checkout, keeping the work and the pairing.

        The conversation keeps its workspace id, so the state this returns is
        the same workspace with nowhere to run -- `attach_workspace` restores
        it. Detaching a conversation that has no workspace does nothing.
        """
        instance = await self._require_instance(instance_id)
        if instance.workspace_id is None:
            return None
        provider = self._capabilities.workspace_provider
        await provider.detach(instance.workspace_id)
        return await provider.state(instance.workspace_id)

    async def _require_instance(self, instance_id: AgentInstanceId) -> AgentInstance:
        instance = await self._capabilities.state_store.load_instance(instance_id)
        if instance is None:
            raise UnknownInstanceError(instance_id)
        return instance

    async def say(
        self,
        instance_id: AgentInstanceId,
        text: str,
        runner: str | None = None,
        on_message: TurnObserver | None = None,
        on_approval: ApprovalHandler | None = None,
        agent_run_id: AgentRunId | None = None,
    ) -> AgentTurn:
        """Add a message to the conversation and get the agent's reply.

        The user's message is stored before the agent runs, so a failed turn
        leaves an accurate transcript -- a question with no answer -- rather
        than losing what was asked.

        `runner` names which one answers, defaulting to the first wired. When
        `on_message` is provided, streaming runners call it for narration, tool
        activity, and the final answer as each becomes available. A non-streaming
        runner still calls it, but only after the complete turn returns.
        `on_approval` is passed to interactive runners so they can pause until
        the caller has presented a request and returned the user's decision; a
        runner that cannot pause is an error rather than a silent downgrade to
        running unattended.

        `agent_run_id` lets a caller that has to name the execution before it
        starts -- because it is brokering approvals for it, or replaying it
        durably -- supply the id instead of learning it afterwards.

        A turn that is cancelled keeps whatever a reporting runner had already
        told us it did, marked as interrupted. A runner that reports nothing
        until it returns leaves nothing to keep, which is why the workspace and
        not this is the last word on what actually happened.
        """
        runner_name = runner or self.default_runner
        if runner_name not in self._runners:
            raise UnknownRunnerError(runner_name, self.runners)
        store = self._capabilities.state_store

        instance = await store.load_instance(instance_id)
        if instance is None:
            raise UnknownInstanceError(instance_id)
        conversation = await store.load_conversation(instance_id)
        if conversation is None:
            raise UnknownInstanceError(instance_id)

        profile = profile_for(instance.agent_id, self._profiles)
        # After the profile, because which runner answers depends on it -- and
        # still before anything is written, so a turn nobody can run leaves the
        # transcript as it was.
        selected_runner = self.runner_for(instance.agent_id, runner_name)
        interactive: InteractiveAgentRunner | None = None
        if on_approval is not None:
            if not isinstance(selected_runner, InteractiveAgentRunner):
                raise ApprovalsUnsupportedError(runner_name)
            interactive = selected_runner
        tools = self._tools_for(profile)

        question = Message.user(text)
        await store.append_messages(instance_id, (question,))

        agent_run = AgentRun(
            agent_run_id=agent_run_id or _new_agent_run_id(),
            instance_id=instance_id,
            status=AgentRunStatus.RUNNING,
            runner=runner_name,
        )
        await store.record_agent_run(agent_run)

        # Kept in case this turn is stopped: a runner that reports as it goes
        # has already told us what the agent did, and the alternative to
        # storing it is a conversation that denies work the workspace still
        # has. Unused when the turn finishes -- `turn.transcript` is
        # authoritative then, and is what gets stored.
        partial: list[Message] = []

        def record(message: Message) -> None:
            partial.append(message)
            if on_message is not None:
                on_message(message)

        try:
            if interactive is not None and on_approval is not None:
                turn = await interactive.run_turn_interactive(
                    agent_run.agent_run_id,
                    profile,
                    (*conversation.messages, question),
                    on_approval,
                    on_message=record,
                    tools=tools,
                    workspace_id=instance.workspace_id,
                )
            elif on_message is not None and isinstance(
                selected_runner, StreamingAgentRunner
            ):
                turn = await selected_runner.run_turn_streamed(
                    agent_run.agent_run_id,
                    profile,
                    (*conversation.messages, question),
                    record,
                    tools=tools,
                    workspace_id=instance.workspace_id,
                )
            else:
                turn = await selected_runner.run_turn(
                    agent_run.agent_run_id,
                    profile,
                    (*conversation.messages, question),
                    tools=tools,
                    workspace_id=instance.workspace_id,
                )
                if on_message is not None:
                    for message in turn.transcript:
                        on_message(message)
        except asyncio.CancelledError:
            # Somebody stopped this on purpose. Recording it as failed would
            # read as the agent breaking, and leaving it running would leave a
            # row that never resolves.
            #
            # The run goes first and the work second, because an await inside a
            # cancelled task can itself be interrupted and only one of these can
            # then happen: a run stuck RUNNING for ever is worse than a partial
            # nobody kept.
            await store.record_agent_run(
                replace(agent_run, status=AgentRunStatus.CANCELLED, summary="cancelled")
            )
            if partial:
                await store.append_messages(
                    instance_id, _interrupted_transcript(partial)
                )
            raise
        except Exception as error:
            await store.record_agent_run(
                replace(
                    agent_run,
                    status=AgentRunStatus.FAILED,
                    summary=f"{type(error).__name__}: {error}",
                )
            )
            raise

        # The whole turn, not just the conclusion: an agent that read three
        # files before answering leaves those reads in the transcript, so a
        # later reader can see why it said what it said.
        await store.append_messages(instance_id, turn.transcript)
        await store.record_agent_run(
            replace(
                agent_run,
                status=AgentRunStatus.SUCCEEDED,
                summary=turn.message.content,
            )
        )
        return turn

    def runner_for(self, agent_id: AgentId, runner: str = "") -> AgentRunner:
        """Which runner answers for this agent, under the name that was picked.

        A profile that only reads is answered by the read-only runner wired
        under that same name: a planner and a coder are the same provider with
        a different set of tools, rather than two providers. Public because a
        caller that runs a turn of its own -- naming a chat, say -- has to get
        the same answer this does, or the restriction would hold for the
        conversation and not for everything done around it.
        """
        runner_name = runner or self.default_runner
        if runner_name not in self._runners:
            raise UnknownRunnerError(runner_name, self.runners)
        profile = profile_for(agent_id, self._profiles)
        if profile.read_only and runner_name in self._read_only_runners:
            return self._read_only_runners[runner_name]
        return self._runners[runner_name]

    def _tools_for(self, profile: AgentProfile) -> tuple[ToolSpec, ...]:
        missing = [grant for grant in profile.capabilities if grant not in self._tools]
        if missing:
            raise UnknownToolGrantError(profile.agent_id, missing)
        return tuple(self._tools[grant] for grant in profile.capabilities)


def _interrupted_transcript(partial: Sequence[Message]) -> tuple[Message, ...]:
    """What a stopped turn leaves behind, in a shape a later turn can read.

    Two things are added and both are added at the end, because a conversation
    is append-only: the calls that never came back are closed off, and the note
    that explains why follows them. Nothing already written is touched, so the
    text this renders to still starts with the text it rendered to before.
    """
    answered = {
        message.tool_call_id
        for message in partial
        if message.role is Role.TOOL and message.tool_call_id
    }
    unanswered = tuple(
        call.call_id
        for message in partial
        for call in message.tool_calls
        if call.call_id not in answered
    )
    return (
        *partial,
        *(
            Message.tool_result(call_id, INTERRUPTED_TOOL_RESULT)
            for call_id in unanswered
        ),
        Message.system(INTERRUPTED_TURN_NOTE),
    )


def _new_agent_run_id() -> AgentRunId:
    """Mint an id for one execution.

    Random, which is fine for an interactive session and wrong under a durable
    workflow runtime: replay has to see the id it saw the first time. When runs
    are started durably the id comes in on the command instead -- which is why
    `StartAgentRun` already carries one.
    """
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


__all__ = [
    "DEFAULT_RUNNER",
    "INTERRUPTED_TOOL_RESULT",
    "INTERRUPTED_TURN_NOTE",
    "AgentSession",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
    "WorkspacesUnavailableError",
]
