"""Run state: the engine's memory between events.

State is data, not behaviour. It is rebuilt by folding events, so it must stay
trivially serialisable -- no handles, no connections, no adapter objects.
"""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.events import HumanReviewCompleted, StepCompleted
from engine.domain.ids import (
    AgentRunId,
    MilestoneId,
    RunId,
    StepId,
    TaskId,
    WorkflowId,
    WorkstreamId,
    WorkspaceId,
)
from engine.domain.workflow import WorkflowDefinition


class RunPhase(Enum):
    """Coarse lifecycle position of a run."""

    PENDING = "pending"
    PREPARING_WORKSPACE = "preparing_workspace"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    RUNNING_AGENT = "running_agent"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunOrigin:
    """Where a run was asked for, and where its progress is reported back.

    Provider-neutral on purpose. `channel` and `thread_id` are whatever the
    communications adapter addresses a conversation by -- a Slack channel and
    the timestamp of the message that started the thread, a Buzz room and a
    post -- and `author` is whoever asked, in that same vocabulary.

    The engine never reads any of it. It travels with the run so the runtime
    can answer in the place the request came from rather than in a channel
    configured once for everything.
    """

    channel: str = ""
    thread_id: str = ""
    author: str = ""


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything the engine needs to decide what happens next."""

    run_id: RunId
    task_id: TaskId
    workflow_id: WorkflowId
    workstream_id: WorkstreamId | None = None
    milestone_id: MilestoneId | None = None
    phase: RunPhase = RunPhase.PENDING
    repository: str = ""
    prompt: str = ""
    name: str = ""
    workspace_id: WorkspaceId | None = None
    agent_runs: tuple[AgentRunId, ...] = field(default=())
    max_agent_runs: int = 3
    current_step_id: StepId | None = None
    current_agent_run_id: AgentRunId | None = None
    agent_paused: bool = False
    """Whether the current agent step intentionally awaits a human continuation."""
    runner_name: str = ""
    """The run's initial provider, used until a conversation selects another."""
    step_results: tuple[StepCompleted, ...] = field(default=())
    human_review: HumanReviewCompleted | None = None
    human_reviews: tuple[HumanReviewCompleted, ...] = field(default=())
    """Every human decision, retained for workflows with more than one review."""
    failure_reason: str = ""
    workflow_definition: WorkflowDefinition | None = None
    """The compiled definition snapshot used by this run."""
    origin: RunOrigin | None = None
    """The conversation this run was requested from, or ``None`` for the web."""

    @property
    def agent_runs_remaining(self) -> int:
        return max(0, self.max_agent_runs - len(self.agent_runs))

    @property
    def is_terminal(self) -> bool:
        return self.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED)


__all__ = ["RunOrigin", "RunPhase", "RunState"]
