"""Agent identity: role, instance, run.

Three levels, and the distinction between them is load-bearing:

    AgentProfile      the logical role. Configuration, not runtime state.
        |             "foreman", "coder", "architecture-reviewer"
        v
    AgentInstance     a durable instance of that role, owning a conversation
        |             and, optionally, a task and a workspace
        v
    AgentRun          one execution of that instance

An instance outlives any single run, which is what lets an agent pause for
clarification and resume later as the same logical entity with the same history.
A provider-side session (a Codex or Claude conversation id) may be associated
with an instance as an adapter optimisation, but it is never the identity: the
`AgentInstanceId` is.

A profile is data, so nothing here decides how the agent is executed. A profile
that talks to a chat model and one that shells out to a coding CLI differ only
in which `engine.ports.AgentRunner` is wired up to run them.
"""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ConversationId,
    RunId,
    StepId,
    TaskId,
    WorkspaceId,
)


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """The definition of an agent role.

    `capabilities` names the tools this agent is granted -- the runtime resolves
    each name to a concrete tool and exposes only those to the model, so a
    profile can be read as the complete statement of what an agent may do. The
    foreman's grants will eventually include dispatch and workflow authoring;
    a reviewer's will not.

    Grants are plain strings rather than an enum because tools are registered by
    the runtime, and the set is expected to grow faster than this module.
    """

    agent_id: AgentId
    instructions: str
    capabilities: tuple[str, ...] = field(default=())
    model: str = ""
    """Preferred model, advisory. Empty means the runner's default."""
    description: str = ""
    """One line, for humans choosing an agent to talk to."""
    read_only: bool = False
    """Whether this role may read a workspace but never change it.

    A statement about the role rather than about one piece of work, which is
    what a conversation has instead of a step: a workflow step declares its own
    `workspace_access`, and nothing here overrides it. The runtime honours this
    by answering the profile with a read-only runner where one is wired, so a
    planner and a coder differ in what they may do and not only in what they
    were told.
    """


class AgentRunStatus(Enum):
    """Lifecycle position of a single execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """A durable instance of an agent role.

    Holds continuity: identity, conversation, user-facing metadata, and what the
    instance is attached to. Interactive coding chats may have a workspace
    without a task; a coder working ENG-42 has both.
    """

    instance_id: AgentInstanceId
    agent_id: AgentId
    conversation_id: ConversationId
    task_id: TaskId | None = None
    workspace_id: WorkspaceId | None = None
    title: str = "New chat"
    archived: bool = False
    runner: str = ""
    auto_approve: bool = False
    """Whether this conversation applies the system auto-approval policy."""
    workflow_run_id: RunId | None = None
    """Owning workflow run, absent for a standalone interactive conversation."""
    workflow_step_id: StepId | None = None
    """Owning workflow step, recorded explicitly rather than parsed from an id."""


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One execution of an instance, and how it turned out.

    Separate from the conversation on purpose: the conversation records what was
    said, an `AgentRun` records what the agent did.
    """

    agent_run_id: AgentRunId
    instance_id: AgentInstanceId
    status: AgentRunStatus = AgentRunStatus.PENDING
    summary: str = ""
    changed_files: tuple[str, ...] = field(default=())
    runner: str = ""
    """Which runner executed this, by the name the composition root gave it.

    Provenance, not configuration: a profile still does not choose its runner,
    but a conversation continued by two different ones should record which
    answered when. A name, never an implementation -- the domain stays unable to
    tell you what "claude" is.
    """

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        )


__all__ = ["AgentInstance", "AgentProfile", "AgentRun", "AgentRunStatus"]
