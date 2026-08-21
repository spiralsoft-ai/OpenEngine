"""Events: things that have already happened.

Events are inputs to the engine. They are facts, stated in the past tense, and
are never speculative -- an adapter emits one only after the world has actually
changed. Compare `commands`, which are requests for change.

Placeholder set for Ticket 1; the real vocabulary lands with the engine itself.
"""

from dataclasses import dataclass, field

from engine.domain.ids import (
    IMPLEMENTATION_REVIEW_WORKFLOW_ID,
    AgentRunId,
    RunId,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.domain.workflow import StepOutput


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every engine input."""

    run_id: RunId


@dataclass(frozen=True, slots=True)
class RunRequested(Event):
    """A human or upstream system asked for work to be done."""

    task_id: TaskId
    prompt: str
    repository: str
    workflow_id: WorkflowId = IMPLEMENTATION_REVIEW_WORKFLOW_ID


@dataclass(frozen=True, slots=True)
class RunNamed(Event):
    """An agent supplied the concise display name for a workflow run."""

    name: str


@dataclass(frozen=True, slots=True)
class WorkspaceProvisioned(Event):
    """A workspace provider handed back a usable checkout."""

    workspace_id: WorkspaceId
    root_path: str


@dataclass(frozen=True, slots=True)
class AgentRunCompleted(Event):
    """An agent runner finished one execution, successfully or not."""

    agent_run_id: AgentRunId
    succeeded: bool
    summary: str
    changed_files: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class StepCompleted(Event):
    """A workflow step finished with an outcome and its declared outputs."""

    step_id: StepId
    agent_run_id: AgentRunId
    outcome: str
    summary: str
    outputs: tuple[StepOutput, ...] = field(default=())
    mcp_request_id: str | int | None = None
    """JSON-RPC request that submitted the result, absent for non-MCP producers."""


@dataclass(frozen=True, slots=True)
class StepReactivated(Event):
    """A human message reopened a previously closed workflow step."""

    step_id: StepId


@dataclass(frozen=True, slots=True)
class HumanReviewCompleted(Event):
    """A human made the final decision for a workflow review step."""

    step_id: StepId
    approved: bool
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ChangesPublished(Event):
    """Source control accepted the attempt's changes."""

    review_url: str


@dataclass(frozen=True, slots=True)
class RunFailed(Event):
    """An unrecoverable failure ended the run."""

    reason: str
    agent_run_id: AgentRunId | None = None
    """The bound agent execution that reported the failure, when applicable."""
    mcp_request_id: str | int | None = None
    """JSON-RPC request that submitted the failure, absent for other failures."""


__all__ = [
    "AgentRunCompleted",
    "ChangesPublished",
    "Event",
    "HumanReviewCompleted",
    "RunFailed",
    "RunNamed",
    "RunRequested",
    "StepCompleted",
    "StepReactivated",
    "WorkspaceProvisioned",
]
