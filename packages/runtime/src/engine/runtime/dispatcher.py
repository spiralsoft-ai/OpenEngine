"""Command dispatch: the one place where decisions become effects.

The engine emits commands. This module is the *only* code that turns them into
calls on real capabilities. Keeping that translation in a single, small,
exhaustive `match` is what lets the boundary be checked mechanically -- if a
command has no arm here, dispatch fails loudly rather than silently doing
nothing.

Ticket 1 ships the seam and its wiring; the per-command bodies fill in alongside
their adapters.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from engine.domain.agents import AgentProfile, AgentRun, AgentRunStatus
from engine.domain.chat import Message
from engine.domain.commands import (
    Command,
    Notify,
    PersistRun,
    ProvisionWorkspace,
    PublishChanges,
    RequestHumanReview,
    ScheduleTimer,
    StartAgentRun,
)
from engine.domain.events import RunFailed, StepCompleted
from engine.domain.ids import ConversationId
from engine.ports import (
    AgentRunner,
    AgentTurn,
    ApprovalHandler,
    InteractiveMcpAgentRunner,
    McpAgentRunner,
    StreamingAgentRunner,
    StreamingMcpAgentRunner,
    TurnObserver,
)
from engine.runtime.capabilities import Capabilities
from engine.runtime.profiles import with_granted_tools
from engine.runtime.step_results import (
    INVALID_COMPLETION_ERROR,
    requests_clarification_or_escalation,
    step_result_instructions,
)
from engine.runtime.terminal_mcp import (
    REPOSITORY_TOOL_METHODS,
    StatusReporter,
    TerminalEvent,
    TerminalMcpBroker,
    TerminalResultRegistry,
    ToolCallLookup,
    terminal_tool_names,
)


#: How many times a workflow agent that ended a turn without a terminal result
#: is told so and asked again. A provider that has ignored the correction twice
#: will not comply on the third pass, and retrying forever would keep spending
#: on a run that cannot finish -- so the step fails with a reason a human can
#: read instead of hanging.
_TERMINAL_RESULT_CORRECTIONS = 2


class UnhandledCommandError(RuntimeError):
    """A command reached dispatch with no capability mapped to it."""

    def __init__(self, command: Command) -> None:
        super().__init__(f"no capability handles {type(command).__name__}")
        self.command = command


class Dispatcher:
    """Executes engine commands against a wired capability set."""

    def __init__(self, capabilities: Capabilities) -> None:
        self._capabilities = capabilities
        self._terminal_results = TerminalResultRegistry()

    async def dispatch_all(self, commands: Iterable[Command]) -> None:
        """Dispatch in order. Sequential by design -- commands from one decision
        may depend on each other, and ordering is the engine's to choose."""
        for command in commands:
            await self.dispatch(command)

    async def dispatch(self, command: Command) -> None:
        caps = self._capabilities
        match command:
            case ProvisionWorkspace():
                await caps.workspace_provider.provision(command.repository, command.base_ref)
            case StartAgentRun():
                if command.step is None:
                    # No `tools=`, so this path turns a profile's grants into
                    # nothing callable and has nothing to announce.
                    # `AgentSession` refuses such a profile outright rather than
                    # run it (`UnknownToolGrantError`); dispatch has no
                    # equivalent, and until it does, silence is the lesser of
                    # the two failures -- an agent told it holds a tool nobody
                    # served reaches for it and gets nowhere.
                    await caps.agent_runner.run_turn(
                        command.agent_run_id,
                        command.profile,
                        (Message.user(command.prompt),),
                        workspace_id=command.workspace_id,
                    )
                else:
                    await self.run_workflow_agent(command)
            case RequestHumanReview():
                # The state store is the durable request. A future ingress may
                # additionally notify an external review system.
                pass
            case PublishChanges():
                await caps.source_control.publish(command.workspace_id, command.branch)
            case Notify():
                await caps.communications.post(command.channel, command.message, command.run_id)
            case PersistRun():
                # Fills in once the runtime threads state through dispatch.
                pass
            case ScheduleTimer():
                await caps.workflow_runtime.schedule_timer(
                    command.run_id, command.delay_seconds, command.reason
                )
            case _:
                raise UnhandledCommandError(command)

    async def run_workflow_agent(
        self,
        command: StartAgentRun,
        runner: AgentRunner | None = None,
        runner_name: str = "",
        on_terminal_result: Callable[[TerminalEvent], Awaitable[None]] | None = None,
        on_approval: ApprovalHandler | None = None,
        continuation: str | None = None,
        on_status: StatusReporter | None = None,
    ) -> AgentTurn | TerminalEvent:
        """Run or continue a workflow step, preferring a delivered MCP result."""
        caps = self._capabilities
        selected_runner = runner or caps.agent_runner
        assert command.step is not None
        # What the chosen branch will actually serve, which is what the step is
        # told it holds. Only the MCP branch serves anything: the other two pass
        # no `tools=`, so a profile's grants resolve to nothing callable there.
        #
        # The terminal tools belong on this list even though no profile grants
        # them -- the broker is what offers them, and `step_result_instructions`
        # naming them in the user prompt does not stop a note introduced as an
        # enumeration from reading as a complete one.
        served: tuple[str, ...] = ()
        reports_status = on_status is not None and isinstance(
            selected_runner, McpAgentRunner
        )
        if isinstance(selected_runner, McpAgentRunner):
            served = terminal_tool_names(
                self.repository_tools(command.profile),
                status_updates=reports_status,
            )
        command = replace(
            command, profile=with_granted_tools(command.profile, served)
        )
        instance = await caps.state_store.create_instance(
            command.profile.agent_id,
            workspace_id=command.workspace_id,
            instance_id=command.instance_id,
            conversation_id=ConversationId(f"{command.instance_id}:conversation"),
            workflow_run_id=command.run_id,
            workflow_step_id=command.step.step_id,
            runner=runner_name,
        )
        conversation = await caps.state_store.load_conversation(instance.instance_id)
        initial_prompt = Message.user(
            f"{command.prompt}\n\n"
            f"{step_result_instructions(command.step, status_updates=reports_status)}"
        )
        if conversation is not None and not conversation.messages:
            await caps.state_store.append_messages(
                instance.instance_id, (initial_prompt,)
            )
            messages = (initial_prompt,)
        elif conversation is not None and continuation is not None:
            follow_up = Message.user(continuation)
            await caps.state_store.append_messages(instance.instance_id, (follow_up,))
            messages = (*conversation.messages, follow_up)
        elif conversation is not None:
            messages = conversation.messages
        else:  # pragma: no cover - stores create the conversation with the instance
            raise RuntimeError(
                f"workflow instance {instance.instance_id} has no conversation"
            )

        agent_run = AgentRun(
            agent_run_id=command.agent_run_id,
            instance_id=instance.instance_id,
            status=AgentRunStatus.RUNNING,
            runner=runner_name,
        )
        await caps.state_store.record_agent_run(agent_run)
        observed: list[Message] = []
        pending: asyncio.Queue[Message | None] = asyncio.Queue()

        def observe(message: Message) -> None:
            observed.append(message)
            pending.put_nowait(message)

        def observed_tool_call_id(name: str, arguments: str) -> str | None:
            """The transcript's id for the call a run-bound tool is answering.

            Read off what the provider has already reported rather than passed
            through the MCP request, because the two are different transports
            and only this one produces the ids the conversation is written in.
            Newest first: an agent that runs the same command twice is asking
            about the second one. Unmatched is `None` rather than a guess -- a
            request beside the wrong call is worse than one beside no call.
            """

            for message in reversed(observed):
                for call in message.tool_calls:
                    if call.name == name and call.arguments == arguments:
                        return call.call_id
            return None

        async def persist_progress() -> None:
            while (message := await pending.get()) is not None:
                await caps.state_store.append_messages(instance.instance_id, (message,))

        progress_task = asyncio.create_task(persist_progress())
        try:
            try:
                if isinstance(selected_runner, McpAgentRunner):
                    result, turn, transcript = await self._run_with_terminal_mcp(
                        selected_runner,
                        command,
                        messages,
                        on_terminal_result,
                        observe,
                        on_approval,
                        observed_tool_call_id,
                        on_status if reports_status else None,
                    )
                elif isinstance(selected_runner, StreamingAgentRunner):
                    result = None
                    turn = await selected_runner.run_turn_streamed(
                        command.agent_run_id,
                        command.profile,
                        messages,
                        observe,
                        workspace_id=command.workspace_id,
                    )
                    transcript = turn.transcript
                else:
                    result = None
                    turn = await selected_runner.run_turn(
                        command.agent_run_id,
                        command.profile,
                        messages,
                        workspace_id=command.workspace_id,
                    )
                    transcript = turn.transcript
            finally:
                pending.put_nowait(None)
                await progress_task
        except asyncio.CancelledError:
            await caps.state_store.record_agent_run(
                replace(agent_run, status=AgentRunStatus.CANCELLED, summary="cancelled")
            )
            raise
        except Exception as error:
            await caps.state_store.record_agent_run(
                replace(
                    agent_run,
                    status=AgentRunStatus.FAILED,
                    summary=f"{type(error).__name__}: {error}",
                )
            )
            raise
        # Streaming runners have already persisted what they observed. The
        # returned turn remains authoritative for anything only synthesized at
        # completion, while terminal cancellation may leave more observed than
        # the partial turn returned by the provider.
        #
        # Matched by identity rather than by position, because a turn is
        # assembled with its last spoken text as the answer -- so narration that
        # streamed before a tool call is reordered to the end, and a streamed
        # message need not sit at the same index in the finished turn. That is
        # presentation, not divergence: those messages are already stored, in
        # the order they were really emitted. Comparing positionally instead
        # read the reorder as a mismatch and failed steps that had completed.
        already_stored = list(observed)
        fresh: list[Message] = []
        for message in transcript:
            if message in already_stored:
                already_stored.remove(message)
            else:
                fresh.append(message)
        unseen = tuple(fresh)
        if unseen:
            await caps.state_store.append_messages(instance.instance_id, unseen)
        if result is not None:
            status = (
                AgentRunStatus.FAILED
                if isinstance(result, RunFailed)
                else AgentRunStatus.SUCCEEDED
            )
            summary = result.reason if isinstance(result, RunFailed) else result.summary
            await caps.state_store.record_agent_run(
                replace(agent_run, status=status, summary=summary)
            )
            return result
        assert turn is not None
        await caps.state_store.record_agent_run(
            replace(
                agent_run,
                status=AgentRunStatus.SUCCEEDED,
                summary=turn.message.content,
            )
        )
        return turn

    def repository_tools(self, profile: AgentProfile) -> tuple[str, ...]:
        """Which repository tools a profile's broker will offer.

        The intersection of two things, because a grant alone is not enough:
        the profile has to ask for the tool, and the composed source control
        has to have the method behind it. A grant against a source control
        that cannot honour it is left off the listing rather than served as
        something that fails when called.

        Asked twice per step -- once to enable them, once to say so in the
        system prompt -- which is the reason it is a method rather than a
        condition written out at each site: the announcement and the listing
        have to agree, and two copies of this are two things to keep in step.
        Public because the naming turn asks the same question of the naming
        profile, and answering it a second way there is the same hazard.
        """
        source_control = self._capabilities.source_control
        return tuple(
            name
            for name, method in REPOSITORY_TOOL_METHODS.items()
            if name in profile.capabilities
            and callable(getattr(source_control, method, None))
        )

    async def _run_with_terminal_mcp(
        self,
        runner: McpAgentRunner,
        command: StartAgentRun,
        messages: tuple[Message, ...],
        deliver: Callable[[TerminalEvent], Awaitable[None]] | None,
        on_message: TurnObserver,
        on_approval: ApprovalHandler | None,
        tool_call_ids: ToolCallLookup | None = None,
        on_status: StatusReporter | None = None,
    ) -> tuple[TerminalEvent | None, AgentTurn | None, tuple[Message, ...]]:
        """Run until a terminal result or clarification request is produced."""
        assert command.step is not None
        broker = TerminalMcpBroker(
            run_id=command.run_id,
            agent_run_id=command.agent_run_id,
            step=command.step,
            registry=self._terminal_results,
            deliver=deliver,
        )
        if on_status is not None:
            # Same tolerance as the repository hook below: a transport fake
            # that models only terminal delivery keeps working.
            enable_status = getattr(broker, "enable_status_updates", None)
            if enable_status is not None:
                enable_status(on_status)
        repository_tools = self.repository_tools(command.profile)
        if repository_tools:
            # Older transport fakes may model only terminal delivery. The real
            # broker exposes this hook; keeping it optional preserves those
            # focused tests while granting a step its repository tools.
            enable = getattr(broker, "enable_repository_tools", None)
            if enable is not None:
                enable(
                    self._capabilities.source_control,
                    repository_tools,
                    command.workspace_id,
                    on_approval,
                    tool_call_ids,
                )
        async with broker:
            transcript: list[Message] = []
            corrections = 0
            run_task: asyncio.Task[AgentTurn] | None = None
            result_task: asyncio.Task[TerminalEvent] | None = None
            try:
                while True:
                    streaming = on_approval is not None or isinstance(
                        runner, StreamingMcpAgentRunner
                    )
                    if on_approval is not None:
                        if not isinstance(runner, InteractiveMcpAgentRunner):
                            raise RuntimeError(
                                "workflow approval handling requires an interactive "
                                "MCP runner"
                            )
                        turn_call = runner.run_turn_with_mcp_interactive(
                            command.agent_run_id,
                            command.profile,
                            messages,
                            broker.config,
                            on_approval,
                            on_message=on_message,
                            workspace_id=command.workspace_id,
                        )
                    elif streaming:
                        turn_call = runner.run_turn_with_mcp_streamed(
                            command.agent_run_id,
                            command.profile,
                            messages,
                            broker.config,
                            on_message,
                            workspace_id=command.workspace_id,
                        )
                    else:
                        turn_call = runner.run_turn_with_mcp(
                            command.agent_run_id,
                            command.profile,
                            messages,
                            broker.config,
                            workspace_id=command.workspace_id,
                        )
                    run_task = asyncio.create_task(turn_call)
                    result_task = asyncio.create_task(broker.result())
                    done, _pending = await asyncio.wait(
                        (run_task, result_task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if result_task in done:
                        result = result_task.result()
                        try:
                            await runner.cancel(command.agent_run_id)
                        except Exception:
                            # Cancellation is best-effort and cannot revoke a
                            # result already accepted and delivered by the runtime.
                            pass
                        turn: AgentTurn | None = None
                        try:
                            turn = await asyncio.wait_for(run_task, timeout=1.0)
                        except asyncio.TimeoutError:
                            run_task.cancel()
                            await asyncio.gather(run_task, return_exceptions=True)
                        except Exception:
                            # A terminated CLI commonly reports a nonzero exit. The
                            # already-delivered terminal event remains authoritative.
                            pass
                        if turn is not None:
                            if not streaming:
                                for message in turn.transcript:
                                    on_message(message)
                            transcript.extend(turn.transcript)
                        return result, turn, tuple(transcript)

                    result_task.cancel()
                    await asyncio.gather(result_task, return_exceptions=True)
                    turn = await run_task
                    if not streaming:
                        for message in turn.transcript:
                            on_message(message)
                    transcript.extend(turn.transcript)
                    if requests_clarification_or_escalation(turn):
                        return None, turn, tuple(transcript)
                    if corrections >= _TERMINAL_RESULT_CORRECTIONS:
                        failure = RunFailed(
                            run_id=command.run_id,
                            reason=(
                                f"the {command.step.step_id} agent ended "
                                f"{corrections + 1} turns without reporting a valid "
                                "terminal result"
                            ),
                            agent_run_id=command.agent_run_id,
                        )
                        # Returned rather than raised: this is the step's result,
                        # so it is recorded and folded like any other failure, and
                        # the transcript that led to it stays with the agent.
                        return failure, turn, tuple(transcript)

                    corrections += 1
                    correction = Message.user(INVALID_COMPLETION_ERROR)
                    transcript.append(correction)
                    on_message(correction)
                    messages = (*messages, *turn.transcript, correction)
            except asyncio.CancelledError:
                try:
                    await runner.cancel(command.agent_run_id)
                except Exception:
                    pass
                for task in (run_task, result_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (run_task, result_task) if task is not None),
                    return_exceptions=True,
                )
                raise


__all__ = ["Dispatcher", "UnhandledCommandError"]
