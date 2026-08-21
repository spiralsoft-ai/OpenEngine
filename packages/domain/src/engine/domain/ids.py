"""Identifier types.

`NewType` rather than bare `str` so a `TaskId` cannot silently be passed where a
`RunId` is expected. No runtime cost.

Three of these spell out the agent identity model, innermost first:

    AgentId          the logical role      ("foreman", "coder")
      -> AgentInstanceId   a durable instance of that role, owning a conversation
        -> AgentRunId      one execution of that instance

That layering is what lets an agent stop for clarification and later continue as
the same logical instance -- see `engine.domain.agents`.
"""

from typing import NewType

TaskId = NewType("TaskId", str)
"""A unit of work requested of the engine, e.g. "fix the flaky auth test"."""

StepId = NewType("StepId", str)
"""One step in a workflow."""

WorkflowId = NewType("WorkflowId", str)
"""A versioned workflow definition."""

# The domain event needs a dependency-safe default: the domain package cannot
# import the engine package that implements the workflow.  The workflow module
# aliases this value as its public WORKFLOW_ID.
IMPLEMENTATION_REVIEW_WORKFLOW_ID = WorkflowId("implementation-review-v1")

RunId = NewType("RunId", str)
"""One end-to-end execution of a `TaskId`."""

AgentId = NewType("AgentId", str)
"""A logical agent role, naming an `AgentProfile`: "foreman", "coder"."""

AgentInstanceId = NewType("AgentInstanceId", str)
"""A durable instance of an agent role. Owns one conversation."""

AgentRunId = NewType("AgentRunId", str)
"""One execution of an `AgentInstanceId`. An instance may run many times."""

ApprovalId = NewType("ApprovalId", str)
"""One request for user consent raised during an `AgentRunId`.

Ours rather than the provider's: Codex and Claude each number their requests
within a session they own, so their ids collide across runs and mean nothing
after the process that issued them is gone.
"""

SessionGrantId = NewType("SessionGrantId", str)
"""One reusable consent, created from an `ApprovalId` and reused after it.

Keyed separately from the approval it came from because it outlives it: the
request was answered once, and the grant is what answers the next one like it.
"""

ConversationId = NewType("ConversationId", str)
"""The message history belonging to one agent instance."""

MessageId = NewType("MessageId", str)
"""One stored message within a conversation."""

WorkspaceId = NewType("WorkspaceId", str)
"""A checked-out, isolated filesystem an agent works in."""

__all__ = [
    "AgentId",
    "AgentInstanceId",
    "AgentRunId",
    "ApprovalId",
    "ConversationId",
    "MessageId",
    "RunId",
    "SessionGrantId",
    "StepId",
    "TaskId",
    "WorkflowId",
    "WorkspaceId",
    "IMPLEMENTATION_REVIEW_WORKFLOW_ID",
]
