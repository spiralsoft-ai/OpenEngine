"""Execute compiled sequential/branching workflows with durable transitions."""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from engine.core import decide
from engine.core.workflow_interpreter import (
    agent_instance_id,
    agent_run_id,
    current_agent_command,
)
from engine.domain import (
    AgentProfile,
    AgentRunId,
    AgentStep,
    AgentStepPaused,
    Command,
    Event,
    HumanReviewCompleted,
    HumanReviewStep,
    Message,
    ProvisionWorkspace,
    RequestHumanReview,
    RunFailed,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    StepId,
    WorkspaceSpec,
    StepReactivated,
    WorkflowDefinition,
    WorkspaceAccess,
    WorkspaceProvisioned,
)
from engine.ports import (
    AgentRunner,
    AgentTurn,
    ApprovalHandler,
    InteractiveMcpAgentRunner,
    McpAgentRunner,
    Message as CommunicationMessage,
    MessageLink,
)
from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher
from engine.runtime.notifications import RunNotifier
from engine.runtime.profiles import with_granted_tools
from engine.runtime.step_results import (
    awaits_human_answer,
    requests_clarification_or_escalation,
)
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry
from engine.runtime.workflows import WorkflowCatalog


logger = logging.getLogger(__name__)
class WorkflowExecutionError(RuntimeError):
    """The workflow or local composition cannot execute the requested transition."""


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    event: Event
    state: RunState
    commands: tuple[Command, ...]


class WorkflowExecutor:
    """Drive a compiled workflow until it pauses or reaches a terminal state."""

    def __init__(
        self,
        capabilities: Capabilities,
        runners: Mapping[str, AgentRunner] | None = None,
        *,
        review_runners: Mapping[str, AgentRunner],
        approval_handler: Callable[[StartAgentRun, str], ApprovalHandler] | None = None,
        catalog: WorkflowCatalog | None = None,
        default_branch: str = "main",
        communications_channel: str = "",
        public_url: str = "",
    ) -> None:
        self._capabilities = capabilities
        self._dispatcher = Dispatcher(capabilities)
        self._runners = dict(runners or {"default": capabilities.agent_runner})
        self._review_runners = dict(review_runners)
        self._approval_handler = approval_handler
        self._catalog = (
            catalog
            if catalog is not None
            else WorkflowCatalog.from_definitions(())
        )
        self._default_branch = default_branch
        self._communications_channel = communications_channel
        self._public_url = public_url.rstrip("/")
        self._notifier = RunNotifier(capabilities.communications, public_url)
        unreviewable = sorted(set(self._runners) - set(self._review_runners))
        if unreviewable:
            raise WorkflowExecutionError(
                f"no review runner for: {', '.join(unreviewable)}"
            )

    @property
    def runners(self) -> tuple[str, ...]:
        return tuple(self._runners)

    @property
    def default_runner(self) -> str:
        return next(iter(self._runners))

    @property
    def catalog(self) -> WorkflowCatalog:
        return self._catalog

    async def start(
        self, initial_event: RunRequested, runner_name: str = ""
    ) -> None:
        """Start and drive any configured workflow."""

        try:
            state = await self._require_state(initial_event.run_id)
            selected_name = self._runner_name(runner_name, state)
            definition = resolve_default_branch(
                self._definition_for(state, initial_event.workflow_id),
                self._default_branch,
            )
            if (
                state.workflow_definition != definition
                or state.runner_name != selected_name
            ):
                state = replace(
                    state,
                    workflow_definition=definition,
                    runner_name=selected_name,
                )
                await self._capabilities.state_store.save(state)
            state, commands = await self._transition(
                state, initial_event, definition, append_event=False
            )
            provision = _only(commands, ProvisionWorkspace)
            workspace = await self._capabilities.workspace_provider.provision(
                provision.repository, provision.base_ref
            )
            state, commands = await self._transition(
                state,
                WorkspaceProvisioned(
                    run_id=state.run_id,
                    workspace_id=workspace.workspace_id,
                    root_path=workspace.root_path,
                ),
                definition,
            )
            state = await self._name_workflow(state, definition, selected_name)
            await self._drive(state, commands, definition, selected_name)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(initial_event.run_id, error)

    async def resume_agent_step(
        self,
        run_id: RunId,
        message: str | None = None,
        runner_name: str = "",
        *,
        step_id: StepId | None = None,
    ) -> None:
        """Reconstruct and run the current agent command after a pause/restart."""

        durable_state = await self._require_state(run_id)
        state = durable_state
        definition = self._definition_for(state)
        target_step_id = step_id or state.current_step_id
        target_step = definition.step(target_step_id) if target_step_id else None
        deferred_event: StepReactivated | None = None
        if message is not None:
            if not isinstance(target_step, AgentStep) or not target_step.editable:
                raise WorkflowExecutionError("workflow step is read-only")
            deferred_event = StepReactivated(
                run_id=run_id, step_id=target_step.step_id
            )
            state, commands = decide(state, deferred_event, definition)
            if commands:
                raise WorkflowExecutionError(
                    "reactivating an agent step unexpectedly emitted commands"
                )
        if state.phase is not RunPhase.RUNNING_AGENT:
            raise WorkflowExecutionError("run is not executing an agent step")
        step = definition.step(state.current_step_id) if state.current_step_id else None
        if not isinstance(step, AgentStep):
            raise WorkflowExecutionError("current workflow step is not an agent step")
        if message is None and state.agent_paused:
            return
        selected_name = await self._runner_name_for_step(
            state, step, runner_name
        )
        try:
            command = current_agent_command(definition, state)
            runner = self._runner_for(step, selected_name)
            outcome = await self._run_step(
                state,
                command,
                definition=definition,
                runner=runner,
                runner_name=selected_name,
                continuation=message,
                deferred_state=(durable_state, deferred_event)
                if deferred_event is not None
                else None,
            )
            if outcome is not None:
                await self._drive(
                    outcome.state,
                    outcome.commands,
                    definition,
                    selected_name,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(run_id, error)

    async def pause_agent_step(self, run_id: RunId, step_id: StepId) -> RunState:
        """Durably mark an interrupted agent step as waiting for continuation."""

        state = await self._require_state(run_id)
        if (
            state.phase is not RunPhase.RUNNING_AGENT
            or state.current_step_id != step_id
            or state.current_agent_run_id is None
        ):
            raise WorkflowExecutionError("workflow step is no longer active")
        definition = self._definition_for(state)
        next_state, commands = await self._transition(
            state,
            AgentStepPaused(
                run_id=run_id,
                step_id=step_id,
                agent_run_id=state.current_agent_run_id,
            ),
            definition,
        )
        if commands:
            raise WorkflowExecutionError("pausing an agent step emitted commands")
        return next_state

    async def complete_human_review(
        self, event: HumanReviewCompleted
    ) -> RunState:
        state = await self._require_state(event.run_id)
        if (
            state.phase is not RunPhase.AWAITING_HUMAN_REVIEW
            or event.step_id != state.current_step_id
        ):
            raise WorkflowExecutionError("run is not awaiting human review")
        definition = self._definition_for(state)
        next_state, commands = await self._transition(state, event, definition)
        if len(commands) > 1:
            raise WorkflowExecutionError(
                "parallel workflow commands are not supported in v1"
            )
        return next_state

    async def _drive(
        self,
        state: RunState,
        commands: tuple[Command, ...],
        definition: WorkflowDefinition,
        runner_name: str,
    ) -> RunState:
        while commands:
            if len(commands) != 1:
                raise WorkflowExecutionError(
                    "parallel workflow commands are not supported in v1"
                )
            command = commands[0]
            if isinstance(command, RequestHumanReview):
                await self._notify_human_review(state, command, definition)
                return state
            if not isinstance(command, StartAgentRun) or command.step is None:
                raise WorkflowExecutionError(
                    f"unsupported workflow command: {type(command).__name__}"
                )
            step = definition.step(command.step.step_id)
            if not isinstance(step, AgentStep):
                raise WorkflowExecutionError(
                    f"agent step not found: {command.step.step_id}"
                )
            selected_name = await self._runner_name_for_step(
                state, step, runner_name
            )
            continuation = (
                command.prompt
                if command.agent_run_id != agent_run_id(state.run_id, step.step_id)
                else None
            )
            if continuation is None:
                # Entering the step rather than carrying one on, which is what
                # makes this the place a run says its review stage has begun.
                await self._notifier.announce(state, f"*{step.name}* started.")
            outcome = await self._run_step(
                state,
                command,
                definition=definition,
                runner=self._runner_for(step, selected_name),
                runner_name=selected_name,
                continuation=continuation,
            )
            if outcome is None:
                return state
            state, commands = outcome.state, outcome.commands
        return state

    async def _notify_human_review(
        self,
        state: RunState,
        command: RequestHumanReview,
        definition: WorkflowDefinition,
    ) -> None:
        """Notify operators without making Slack availability block a workflow."""
        step = definition.step(command.step_id)
        if not isinstance(step, HumanReviewStep) or step.notification is None:
            return
        pull_request_url = _pull_request_url(state)
        outcome = state.step_results[-1].outcome if state.step_results else "unknown"
        links = []
        if pull_request_url:
            links.append(MessageLink("Open pull request", pull_request_url))
        if state.origin is not None:
            # The run was asked for in a conversation, so the person who asked
            # is told there, by name: this is the point the work stops needing
            # an agent and starts needing them.
            work_order = self._notifier.work_order_link(state)
            if work_order is not None:
                links.append(work_order)
            await self._notifier.announce(
                state,
                f"Review complete and ready for your decision: {command.title}\n"
                f"Outcome: {outcome}",
                links=links,
                mention=True,
            )
            return
        if not self._communications_channel or not self._public_url:
            return
        links.append(
            MessageLink(
                "Open human review task",
                f"{self._public_url}/runs/{command.run_id}",
            )
        )
        message_text = f"Ready for human review: {command.title}\nOutcome: {outcome}"
        message = CommunicationMessage(message_text, tuple(links))
        try:
            await self._capabilities.communications.post(
                self._communications_channel,
                message,
                command.run_id,
            )
        except Exception:
            logger.exception(
                "could not send human-review notification for run %s",
                command.run_id,
            )

    async def _runner_name_for_step(
        self, state: RunState, step: AgentStep, fallback: str
    ) -> str:
        """Prefer a step conversation's choice without leaking it to the next step."""

        instance = await self._capabilities.state_store.load_instance(
            agent_instance_id(state.run_id, step.step_id)
        )
        selected = (
            instance.runner
            if instance is not None and instance.runner
            else state.runner_name or fallback
        )
        return self._runner_name(selected, state)

    async def _name_workflow(
        self,
        state: RunState,
        definition: WorkflowDefinition,
        runner_name: str,
    ) -> RunState:
        if definition.naming_profile is None or not definition.naming_prompt:
            return state
        try:
            turn = await self._naming_turn(
                state, definition.naming_profile, definition.naming_prompt, runner_name
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unnamed run is the right fallback -- naming is not the work --
            # but it is not a silent one. The naming turn is neither an agent
            # run nor a conversation, so nothing else records it: without this
            # line an unauthenticated `gh`, an issue that does not exist and a
            # provider that could not attach the server are indistinguishable
            # from a run whose prompt simply made a good enough name.
            logger.exception("could not name run %s", state.run_id)
            return state
        name = _clean_workflow_name(turn.message.content)
        if not name:
            # The other way to end up unnamed, and now the likelier one: a turn
            # that used tools can answer with something `_clean_workflow_name`
            # keeps nothing of. Logged with what was said, because unlike the
            # raising path there is no exception to describe it.
            logger.warning(
                "run %s was not named; the naming turn answered %r",
                state.run_id,
                turn.message.content,
            )
            return state
        named, commands = await self._transition(
            state, RunNamed(run_id=state.run_id, name=name), definition
        )
        if commands:
            raise WorkflowExecutionError("naming a workflow emitted commands")
        return named

    async def _naming_turn(
        self,
        state: RunState,
        profile: AgentProfile,
        prompt: str,
        runner_name: str,
    ) -> AgentTurn:
        """Name a run, with the repository tools its naming profile is granted.

        Naming happens once the workspace exists, so a granted profile can read
        what the request points at rather than paraphrase the request: "Resolve
        issue 270" is worth a name only after somebody has read issue 270.

        The server bound here serves those tools and nothing else -- this turn
        is not a step, so there is no step for `complete_step` to complete. A
        profile that is granted nothing servable runs as it always has, with no
        server at all.
        """

        runner = self._runners[runner_name]
        agent_run_id = AgentRunId(f"{state.run_id}:name:run")
        messages = (Message.user(state.prompt), Message.user(prompt))
        served = (
            self._dispatcher.repository_tools(profile)
            if isinstance(runner, McpAgentRunner)
            else ()
        )
        if not served:
            return await runner.run_turn(
                agent_run_id, profile, messages, workspace_id=state.workspace_id
            )
        async with TerminalMcpBroker(
            run_id=state.run_id,
            agent_run_id=agent_run_id,
            step=None,
            # Its own registry: the guard is against a second terminal result
            # for one agent run, and this session serves no way to submit one.
            registry=TerminalResultRegistry(),
        ) as broker:
            broker.enable_repository_tools(
                self._capabilities.source_control, served, state.workspace_id
            )
            return await runner.run_turn_with_mcp(
                agent_run_id,
                with_granted_tools(profile, served),
                messages,
                broker.config,
                workspace_id=state.workspace_id,
            )

    async def _run_step(
        self,
        state: RunState,
        command: StartAgentRun,
        *,
        definition: WorkflowDefinition,
        runner: AgentRunner,
        runner_name: str,
        continuation: str | None = None,
        deferred_state: tuple[RunState, Event] | None = None,
    ) -> _StepOutcome | None:
        assert command.step is not None
        folded: _StepOutcome | None = None
        step = definition.step(command.step.step_id)
        step_name = step.name if step is not None else str(command.step.step_id)

        async def report_status(status: str) -> None:
            await self._notifier.announce(state, f"*{step_name}*: {status}")

        async def fold(event: Event) -> _StepOutcome:
            transition_state = state
            if deferred_state is not None:
                durable_state, deferred_event = deferred_state
                transition_state, commands = await self._transition(
                    durable_state, deferred_event, definition
                )
                if commands or transition_state != state:
                    raise WorkflowExecutionError(
                        "deferred step reactivation did not produce the expected state"
                    )
            next_state, commands = await self._transition(
                transition_state, event, definition
            )
            return _StepOutcome(event, next_state, commands)

        async def deliver_terminal(event: Event) -> None:
            nonlocal folded
            folded = await fold(event)

        terminal = await self._dispatcher.run_workflow_agent(
            command,
            runner=runner,
            runner_name=runner_name,
            on_terminal_result=deliver_terminal,
            on_approval=(
                self._approval_handler(command, runner_name)
                if self._approval_handler is not None
                and isinstance(runner, InteractiveMcpAgentRunner)
                else None
            ),
            continuation=continuation,
            on_status=report_status if state.origin is not None else None,
        )
        if folded is not None:
            assert terminal == folded.event
            await self._announce_result(folded, step_name)
            return folded
        if isinstance(terminal, (StepCompleted, RunFailed)):
            outcome = await fold(terminal)
            await self._announce_result(outcome, step_name)
            return outcome
        if requests_clarification_or_escalation(terminal):
            if deferred_state is None:
                await self._transition(
                    state,
                    AgentStepPaused(
                        run_id=state.run_id,
                        step_id=command.step.step_id,
                        agent_run_id=command.agent_run_id,
                    ),
                    definition,
                )
            if awaits_human_answer(terminal):
                # `clarify` is reported by the tool server as it is called, so
                # only a genuine question is announced here -- otherwise the
                # same pause would be said twice, and the second time wrongly.
                await self._notifier.announce(
                    state,
                    f"*{step_name}* is waiting for an answer.\n"
                    f"{terminal.message.content}".strip(),
                    links=_links(self._notifier.work_order_link(state)),
                )
            return None
        raise WorkflowExecutionError(
            f"{command.step.step_id} runner exited without a valid completion state"
        )

    async def _announce_result(self, outcome: _StepOutcome, step_name: str) -> None:
        """Report a step's ending in the conversation that asked for the run.

        Read off the folded state rather than the event, so the pull request
        named here is whichever output the step actually declared it under and
        the message cannot drift from what the run recorded.
        """
        state = outcome.state
        if isinstance(outcome.event, RunFailed):
            await self._notifier.announce(
                state,
                f"*{step_name}* failed.\n{outcome.event.reason}".strip(),
                links=_links(self._notifier.work_order_link(state)),
            )
            return
        if not isinstance(outcome.event, StepCompleted):
            return
        links = []
        pull_request_url = _pull_request_url(state)
        if pull_request_url:
            links.append(MessageLink("Open pull request", pull_request_url))
        work_order = self._notifier.work_order_link(state)
        if work_order is not None:
            links.append(work_order)
        await self._notifier.announce(
            state,
            f"*{step_name}* complete.\n{outcome.event.summary}".strip(),
            links=links,
        )

    async def _transition(
        self,
        state: RunState,
        event: Event,
        definition: WorkflowDefinition,
        *,
        append_event: bool = True,
    ) -> tuple[RunState, tuple[Command, ...]]:
        next_state, commands = decide(state, event, definition)
        if append_event:
            await self._capabilities.state_store.append_events(state.run_id, (event,))
        await self._capabilities.state_store.save(next_state)
        return next_state, commands

    def _definition_for(
        self, state: RunState, requested_id=None
    ) -> WorkflowDefinition:
        if state.workflow_definition is not None:
            return state.workflow_definition
        workflow_id = requested_id or state.workflow_id
        try:
            return self._catalog.require(workflow_id)
        except ValueError as error:
            raise WorkflowExecutionError(str(error)) from error

    def _runner_name(self, runner_name: str, state: RunState | None = None) -> str:
        selected = runner_name or (state.runner_name if state is not None else "")
        selected = selected or self.default_runner
        if selected not in self._runners:
            raise WorkflowExecutionError(f"unknown workflow runner: {selected}")
        return selected

    def _runner_for(self, step: AgentStep, runner_name: str) -> AgentRunner:
        mapping = (
            self._runners
            if step.workspace_access is WorkspaceAccess.WRITE
            else self._review_runners
        )
        try:
            return mapping[runner_name]
        except KeyError as error:
            raise WorkflowExecutionError(
                f"no {step.workspace_access.value} runner for: {runner_name}"
            ) from error

    async def _require_state(self, run_id: RunId) -> RunState:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            raise WorkflowExecutionError(f"run not found: {run_id}")
        return state

    async def _fail(self, run_id: RunId, error: Exception) -> None:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            return
        definition = self._definition_for(state)
        failure = RunFailed(run_id=run_id, reason=f"{type(error).__name__}: {error}")
        failed, _commands = await self._transition(state, failure, definition)
        # The run died somewhere other than a step's own ending, so nothing
        # else will say so -- and a thread that simply goes quiet is the one
        # outcome a person cannot tell from work still in progress.
        await self._notifier.announce(
            failed,
            f"This work order failed.\n{failure.reason}",
            links=_links(self._notifier.work_order_link(failed)),
        )


def _pull_request_url(state: RunState) -> str:
    """The newest `pr_url` any completed step declared, or empty."""
    return next(
        (
            output.value
            for result in reversed(state.step_results)
            for output in result.outputs
            if output.name == "pr_url" and output.value
        ),
        "",
    )


def _links(link: MessageLink | None) -> tuple[MessageLink, ...]:
    return (link,) if link is not None else ()


def _only(commands: Sequence[Command], expected: type[Command]) -> Command:
    if len(commands) != 1 or not isinstance(commands[0], expected):
        names = ", ".join(type(command).__name__ for command in commands) or "none"
        raise WorkflowExecutionError(
            f"expected one {expected.__name__} command, got {names}"
        )
    return commands[0]


def resolve_default_branch(
    definition: WorkflowDefinition, default_branch: str
) -> WorkflowDefinition:
    """Snapshot the configured branch before the pure engine sees a run."""
    if definition.workspace.base_ref:
        return definition
    return replace(
        definition,
        workspace=WorkspaceSpec(base_ref=f"origin/{default_branch}"),
    )


def _clean_workflow_name(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    # Long enough for a name that leads with an issue number and then says what
    # the issue is; the guard is against an agent answering with a paragraph,
    # not against a name a person would recognise.
    return first_line.strip(" \t\"'`).:;!?")[:120]


__all__ = ["WorkflowExecutionError", "WorkflowExecutor", "resolve_default_branch"]
