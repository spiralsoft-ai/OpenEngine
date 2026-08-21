"""Run state: the engine's memory between events.

State is data, not behaviour. It is rebuilt by folding events, so it must stay
trivially serialisable -- no handles, no connections, no adapter objects.
"""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.events import HumanReviewCompleted, StepCompleted
from engine.domain.ids import (
    IMPLEMENTATION_REVIEW_WORKFLOW_ID,
    AgentRunId,
    RunId,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)


class RunPhase(Enum):
    """Coarse lifecycle position of a run."""

    PENDING = "pending"
    PREPARING_WORKSPACE = "preparing_workspace"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    RUNNING_AGENT = "running_agent"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything the engine needs to decide what happens next."""

    run_id: RunId
    task_id: TaskId
    workflow_id: WorkflowId = IMPLEMENTATION_REVIEW_WORKFLOW_ID
    phase: RunPhase = RunPhase.PENDING
    repository: str = ""
    prompt: str = ""
    name: str = ""
    workspace_id: WorkspaceId | None = None
    agent_runs: tuple[AgentRunId, ...] = field(default=())
    max_agent_runs: int = 3
    current_step_id: StepId | None = None
    current_agent_run_id: AgentRunId | None = None
    step_results: tuple[StepCompleted, ...] = field(default=())
    human_review: HumanReviewCompleted | None = None
    failure_reason: str = ""

    @property
    def agent_runs_remaining(self) -> int:
        return max(0, self.max_agent_runs - len(self.agent_runs))

    @property
    def is_terminal(self) -> bool:
        return self.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED)


__all__ = ["RunPhase", "RunState"]
