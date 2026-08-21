"""Run-oriented read model for the implementation-review workflow."""

from dataclasses import dataclass, field

from engine.core.workflows.implementation_review import (
    HUMAN_REVIEW_STEP,
    IMPLEMENTATION_PROFILE,
    IMPLEMENTATION_STEP,
    REVIEW_PROFILE,
    REVIEW_STEP,
    WORKFLOW_ID,
    human_review_summary,
)
from engine.domain.agents import AgentInstance
from engine.domain.approvals import ApprovalStatus
from engine.domain.events import StepCompleted
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ConversationId,
    RunId,
    StepId,
)
from engine.domain.state import RunPhase, RunState
from engine.domain.workflow import StepOutput
from engine.ports.state_store import StateStore
from engine.runtime.step_results import latest_turn_requests_clarification_or_escalation


@dataclass(frozen=True, slots=True)
class RunStepView:
    step_id: StepId
    name: str
    kind: str
    status: str
    outcome: str | None = None
    summary: str = ""
    outputs: tuple[StepOutput, ...] = field(default=())
    changes_requested: bool = False
    agent_id: AgentId | None = None
    agent_instance_id: AgentInstanceId | None = None
    agent_run_id: AgentRunId | None = None
    mcp_request_id: str | int | None = None
    conversation_id: ConversationId | None = None
    waiting: bool = False


@dataclass(frozen=True, slots=True)
class PendingHumanReviewView:
    step_id: StepId
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class HumanDecisionView:
    step_id: StepId
    approved: bool
    outcome: str
    summary: str


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    run_id: RunId
    name: str
    workflow_id: str
    workflow_name: str
    workflow_version: str
    task_id: str
    task_prompt: str
    repository: str
    phase: str
    current_step_id: StepId | None
    terminal_outcome: str | None
    steps: tuple[RunStepView, ...]
    pending_human_review: PendingHumanReviewView | None = None
    human_decision: HumanDecisionView | None = None
    failure_reason: str = ""


class RunReader:
    """Build complete run views from one durable store boundary."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def list(self) -> tuple[WorkflowRunView, ...]:
        return tuple([await self._view(state) for state in await self._store.list_runs()])

    async def get(self, run_id: RunId) -> WorkflowRunView | None:
        state = await self._store.load(run_id)
        return await self._view(state) if state is not None else None

    async def _view(self, state: RunState) -> WorkflowRunView:
        instances = await self._store.list_instances(workflow_run_id=state.run_id)
        by_step = {
            instance.workflow_step_id: instance
            for instance in instances
            if instance.workflow_step_id is not None
        }
        results = {result.step_id: result for result in state.step_results}
        waiting_step = await self._waiting_step(state, by_step)
        steps = (
            _agent_step(
                state,
                IMPLEMENTATION_STEP,
                "Implementation",
                IMPLEMENTATION_PROFILE.agent_id,
                by_step.get(IMPLEMENTATION_STEP),
                results.get(IMPLEMENTATION_STEP),
                waiting=waiting_step == IMPLEMENTATION_STEP,
            ),
            _agent_step(
                state,
                REVIEW_STEP,
                "Review",
                REVIEW_PROFILE.agent_id,
                by_step.get(REVIEW_STEP),
                results.get(REVIEW_STEP),
                waiting=waiting_step == REVIEW_STEP,
            ),
            _human_step(state),
        )
        implementation = results.get(IMPLEMENTATION_STEP)
        review = results.get(REVIEW_STEP)
        pending = None
        if (
            state.phase is RunPhase.AWAITING_HUMAN_REVIEW
            and implementation is not None
            and review is not None
        ):
            pending = PendingHumanReviewView(
                step_id=HUMAN_REVIEW_STEP,
                title=f"Review implementation for {state.task_id}",
                summary=human_review_summary(implementation, review),
            )
        decision = None
        if state.human_review is not None:
            decision = HumanDecisionView(
                step_id=state.human_review.step_id,
                approved=state.human_review.approved,
                outcome="approved" if state.human_review.approved else "rejected",
                summary=state.human_review.summary,
            )
        return WorkflowRunView(
            run_id=state.run_id,
            name=state.name or state.prompt or str(state.run_id),
            workflow_id=str(state.workflow_id),
            workflow_name=(
                "Implementation review"
                if state.workflow_id == WORKFLOW_ID
                else str(state.workflow_id)
            ),
            workflow_version=("v1" if state.workflow_id == WORKFLOW_ID else ""),
            task_id=str(state.task_id),
            task_prompt=state.prompt,
            repository=state.repository,
            phase=state.phase.value,
            current_step_id=state.current_step_id,
            terminal_outcome=_terminal_outcome(state),
            steps=steps,
            pending_human_review=pending,
            human_decision=decision,
            failure_reason=state.failure_reason,
        )

    async def _waiting_step(
        self,
        state: RunState,
        instances: dict[StepId, AgentInstance],
    ) -> StepId | None:
        step_id = state.current_step_id
        if step_id not in (IMPLEMENTATION_STEP, REVIEW_STEP):
            return None
        instance = instances.get(step_id)
        if instance is None:
            return None
        if state.current_agent_run_id is not None:
            approvals = await self._store.list_approvals(
                agent_run_id=state.current_agent_run_id,
                status=ApprovalStatus.PENDING,
            )
            if approvals:
                return step_id
        conversation = await self._store.load_conversation(instance.instance_id)
        if conversation is not None and latest_turn_requests_clarification_or_escalation(
            conversation.messages
        ):
            return step_id
        return None


def _agent_step(
    state: RunState,
    step_id: StepId,
    name: str,
    agent_id: AgentId,
    instance: AgentInstance | None,
    result: StepCompleted | None,
    *,
    waiting: bool = False,
) -> RunStepView:
    status = _step_status(state, step_id, result is not None)
    return RunStepView(
        step_id=step_id,
        name=name,
        kind="agent",
        status=status,
        outcome=result.outcome if result is not None else None,
        summary=(
            result.summary
            if result is not None
            else state.failure_reason if status == "failed" else ""
        ),
        outputs=result.outputs if result is not None else (),
        changes_requested=(
            result is not None and result.outcome == "changes_requested"
        ),
        agent_id=agent_id,
        agent_instance_id=instance.instance_id if instance else None,
        agent_run_id=(
            result.agent_run_id
            if result is not None
            else state.current_agent_run_id if state.current_step_id == step_id else None
        ),
        mcp_request_id=result.mcp_request_id if result is not None else None,
        conversation_id=instance.conversation_id if instance else None,
        waiting=waiting,
    )


def _human_step(state: RunState) -> RunStepView:
    if state.human_review is not None:
        status = "completed"
        outcome = "approved" if state.human_review.approved else "rejected"
        summary = state.human_review.summary
    elif state.phase is RunPhase.AWAITING_HUMAN_REVIEW:
        status, outcome, summary = "action_required", None, "Human decision required."
    else:
        status, outcome, summary = _step_status(state, HUMAN_REVIEW_STEP, False), None, ""
    return RunStepView(
        step_id=HUMAN_REVIEW_STEP,
        name="Human review",
        kind="human",
        status=status,
        outcome=outcome,
        summary=summary,
    )


def _step_status(state: RunState, step_id: StepId, completed: bool) -> str:
    if completed:
        return "completed"
    if state.current_step_id != step_id:
        return "pending"
    if state.phase is RunPhase.FAILED:
        return "failed"
    if state.phase is RunPhase.AWAITING_HUMAN_REVIEW:
        return "action_required"
    if state.is_terminal:
        return "completed"
    return "in_progress"


def _terminal_outcome(state: RunState) -> str | None:
    if state.human_review is not None:
        return "approved" if state.human_review.approved else "rejected"
    if state.phase is RunPhase.SUCCEEDED:
        return "succeeded"
    if state.phase is RunPhase.FAILED:
        return "failed"
    return None


__all__ = [
    "HumanDecisionView",
    "PendingHumanReviewView",
    "RunReader",
    "RunStepView",
    "WorkflowRunView",
]
