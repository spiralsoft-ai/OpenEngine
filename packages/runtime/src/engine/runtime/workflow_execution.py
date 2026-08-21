"""Execute the locally supported portion of a workflow run."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from engine.core import decide
from engine.core.workflows.implementation_review import (
    IMPLEMENTATION_STEP,
    WORKFLOW_NAME_PROMPT,
    WORKFLOW_NAMING_PROFILE,
    start_implementation_command,
    start_review_command,
)
from engine.domain import (
    AgentRunId,
    Command,
    Event,
    HumanReviewCompleted,
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
    StepReactivated,
    WorkspaceProvisioned,
)
from engine.ports import AgentRunner, ApprovalHandler, InteractiveMcpAgentRunner
from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher
from engine.runtime.step_results import requests_clarification_or_escalation


class WorkflowExecutionError(RuntimeError):
    """The reducer emitted a command the local execution slice cannot handle."""


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    """One executed step's terminal event, and what the reducer made of it."""

    event: Event
    state: RunState
    commands: tuple[Command, ...]


class WorkflowExecutor:
    """Drive request, workspace, implementation, and review.

    Agent execution stops where a person has to decide. Human-review ingress
    then calls ``complete_human_review`` so the same reducer owns the terminal
    transition too.

    Two runner mappings, keyed by the same provider names. `runners` may write
    inside the workspace and implements; `review_runners` may only read it and
    reviews what was implemented. The review profile also says not to modify
    anything, but an instruction the model may ignore is not a control -- the
    reviewer is handed a runner that cannot edit the tree in the first place.
    """

    def __init__(
        self,
        capabilities: Capabilities,
        runners: Mapping[str, AgentRunner] | None = None,
        *,
        review_runners: Mapping[str, AgentRunner],
        approval_handler: Callable[[StartAgentRun, str], ApprovalHandler] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._dispatcher = Dispatcher(capabilities)
        self._runners = dict(runners or {"default": capabilities.agent_runner})
        # Every implementation runner must have an explicitly wired reviewer.
        # A missing reviewer is a wiring mistake, and finding it at startup is
        # better than silently reviewing with write access.
        self._review_runners = dict(review_runners)
        self._approval_handler = approval_handler
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

    async def advance_through_review(
        self, initial_event: RunRequested, runner_name: str = ""
    ) -> None:
        """Advance a persisted pending request through implementation and review."""
        try:
            selected_name = runner_name or self.default_runner
            try:
                runner = self._runners[selected_name]
                reviewer = self._review_runners[selected_name]
            except KeyError as error:
                raise WorkflowExecutionError(
                    f"unknown workflow runner: {selected_name}"
                ) from error
            state = await self._require_state(initial_event.run_id)
            state, commands = await self._transition(
                state, initial_event, append_event=False
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
            )
            state = await self._name_workflow(state, runner)
            implementation = await self._run_step(
                state,
                _only(commands, StartAgentRun),
                runner=runner,
                runner_name=selected_name,
            )
            if implementation is None:
                # The step paused for clarification. Its conversation is
                # durable and a later response can resume the same instance.
                return
            await self._advance_after_implementation(
                implementation, reviewer=reviewer, runner_name=selected_name
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(initial_event.run_id, error)

    async def resume_implementation(
        self, run_id: RunId, message: str, runner_name: str = ""
    ) -> None:
        """Continue an editable implementation step with a human message."""

        durable_state = await self._require_state(run_id)
        state = durable_state
        deferred_event: StepReactivated | None = None
        if (
            state.phase is not RunPhase.IMPLEMENTING
            or state.current_step_id != IMPLEMENTATION_STEP
        ):
            deferred_event = StepReactivated(
                run_id=run_id, step_id=IMPLEMENTATION_STEP
            )
            state, commands = decide(state, deferred_event)
            if commands:
                raise WorkflowExecutionError(
                    "reactivating implementation unexpectedly emitted commands"
                )
            if (
                state.phase is not RunPhase.IMPLEMENTING
                or state.current_step_id != IMPLEMENTATION_STEP
            ):
                raise WorkflowExecutionError("run is not implementing")
        try:
            selected_name = runner_name or self.default_runner
            try:
                runner = self._runners[selected_name]
                reviewer = self._review_runners[selected_name]
            except KeyError as error:
                raise WorkflowExecutionError(
                    f"unknown workflow runner: {selected_name}"
                ) from error
            implementation = await self._run_step(
                state,
                start_implementation_command(state),
                runner=runner,
                runner_name=selected_name,
                continuation=message,
                deferred_state=(durable_state, deferred_event)
                if deferred_event is not None
                else None,
            )
            if implementation is None:
                return
            await self._advance_after_implementation(
                implementation, reviewer=reviewer, runner_name=selected_name
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(run_id, error)

    async def _name_workflow(
        self, state: RunState, runner: AgentRunner
    ) -> RunState:
        """Name a submitted run without making cosmetic failure fail its work."""

        try:
            turn = await runner.run_turn(
                AgentRunId(f"{state.run_id}:name:run"),
                WORKFLOW_NAMING_PROFILE,
                (Message.user(state.prompt), Message.user(WORKFLOW_NAME_PROMPT)),
                workspace_id=state.workspace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return state
        name = _clean_workflow_name(turn.message.content)
        if not name:
            return state
        named, commands = await self._transition(
            state, RunNamed(run_id=state.run_id, name=name)
        )
        if commands:
            raise WorkflowExecutionError("naming a workflow emitted commands")
        return named

    async def _advance_after_implementation(
        self,
        implementation: _StepOutcome,
        *,
        reviewer: AgentRunner,
        runner_name: str,
    ) -> None:
        if implementation.state.phase is RunPhase.FAILED:
            return
        if implementation.state.phase is not RunPhase.REVIEWING:
            raise WorkflowExecutionError(
                "implementation result did not advance the run to review"
            )
        review_command = _only(implementation.commands, StartAgentRun)
        review = await self._run_step(
            implementation.state,
            review_command,
            runner=reviewer,
            runner_name=runner_name,
            # Ignored when the first review creates its conversation. On a
            # reactivated implementation, the durable review conversation
            # already exists and needs the new result appended as fresh context.
            continuation=review_command.prompt,
        )
        if review is None or review.state.phase is RunPhase.FAILED:
            return
        if review.state.phase is not RunPhase.AWAITING_HUMAN_REVIEW:
            raise WorkflowExecutionError(
                "review result did not advance the run to human review"
            )
        # Nothing to dispatch: the persisted AWAITING_HUMAN_REVIEW transition
        # is the request for a human. Keep the executor/reducer contract explicit.
        _only(review.commands, RequestHumanReview)

    async def resume_review(self, run_id: RunId, runner_name: str = "") -> None:
        """Resume the review command lost when its process stopped.

        A successful implementation and the resulting ``REVIEWING`` state are
        durable, but command dispatch is process-local. Reconstructing the
        deterministic command lets startup finish that transition without
        rerunning the implementation.
        """
        try:
            selected_name = runner_name or self.default_runner
            try:
                reviewer = self._review_runners[selected_name]
            except KeyError as error:
                raise WorkflowExecutionError(
                    f"unknown workflow runner: {selected_name}"
                ) from error
            state = await self._require_state(run_id)
            if state.phase is not RunPhase.REVIEWING:
                return
            implementation = next(
                (
                    result
                    for result in reversed(state.step_results)
                    if result.step_id == IMPLEMENTATION_STEP
                ),
                None,
            )
            if implementation is None:
                raise WorkflowExecutionError(
                    "reviewing run has no implementation result"
                )
            review = await self._run_step(
                state,
                start_review_command(state, implementation),
                runner=reviewer,
                runner_name=selected_name,
            )
            if review is None or review.state.phase is RunPhase.FAILED:
                return
            if review.state.phase is not RunPhase.AWAITING_HUMAN_REVIEW:
                raise WorkflowExecutionError(
                    "review result did not advance the run to human review"
                )
            _only(review.commands, RequestHumanReview)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(run_id, error)

    async def complete_human_review(
        self, event: HumanReviewCompleted
    ) -> RunState:
        """Persist the final human decision and finish the workflow run."""
        state = await self._require_state(event.run_id)
        if (
            state.phase is not RunPhase.AWAITING_HUMAN_REVIEW
            or event.step_id != state.current_step_id
        ):
            raise WorkflowExecutionError("run is not awaiting human review")
        next_state, commands = await self._transition(state, event)
        if commands:
            raise WorkflowExecutionError(
                "human review unexpectedly emitted follow-up commands"
            )
        return next_state

    async def _run_step(
        self,
        state: RunState,
        command: StartAgentRun,
        *,
        runner: AgentRunner,
        runner_name: str,
        continuation: str | None = None,
        deferred_state: tuple[RunState, Event] | None = None,
    ) -> _StepOutcome | None:
        """Run one agent-backed step and fold its terminal result into the run.

        None means the agent validly paused without completing the step. Its
        conversation is durable and the run stays on this step, so a later
        response can resume the same logical instance.
        """
        assert command.step is not None
        folded: _StepOutcome | None = None

        async def fold(event: Event) -> _StepOutcome:
            transition_state = state
            if deferred_state is not None:
                durable_state, deferred_event = deferred_state
                transition_state, commands = await self._transition(
                    durable_state, deferred_event
                )
                if commands or transition_state != state:
                    raise WorkflowExecutionError(
                        "deferred step reactivation did not produce the expected state"
                    )
            next_state, commands = await self._transition(transition_state, event)
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
        )
        if folded is not None:
            # The MCP acknowledgement was sent only after this transition
            # persisted the event. Never infer delivery from CLI transcript.
            assert terminal == folded.event
            return folded
        if isinstance(terminal, (StepCompleted, RunFailed)):
            return await fold(terminal)
        if requests_clarification_or_escalation(terminal):
            return None
        raise WorkflowExecutionError(
            f"{command.step.step_id} runner exited without a valid completion state"
        )

    async def _transition(
        self,
        state: RunState,
        event: Event,
        *,
        append_event: bool = True,
    ) -> tuple[RunState, tuple[Command, ...]]:
        next_state, commands = decide(state, event)
        if append_event:
            await self._capabilities.state_store.append_events(state.run_id, (event,))
        await self._capabilities.state_store.save(next_state)
        return next_state, commands

    async def _require_state(self, run_id: RunId) -> RunState:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            raise WorkflowExecutionError(f"run not found: {run_id}")
        return state

    async def _fail(self, run_id: RunId, error: Exception) -> None:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            return
        failure = RunFailed(
            run_id=run_id,
            reason=f"{type(error).__name__}: {error}",
        )
        await self._transition(state, failure)


def _only(
    commands: Sequence[Command], expected: type[Command]
) -> Command:
    if len(commands) != 1 or not isinstance(commands[0], expected):
        names = ", ".join(type(command).__name__ for command in commands) or "none"
        raise WorkflowExecutionError(
            f"expected one {expected.__name__} command, got {names}"
        )
    return commands[0]


def _clean_workflow_name(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    return first_line.strip(" \t\"'`).:;!?")[:80]


__all__ = ["WorkflowExecutionError", "WorkflowExecutor"]
