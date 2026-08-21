"""Durable workflow-run and conversation persistence backed by SQLite."""

from collections.abc import Sequence
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4
import warnings

from engine.domain.agents import AgentInstance, AgentProfile, AgentRun, AgentRunStatus
from engine.domain.approvals import (
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    SessionGrant,
)
from engine.domain.chat import Conversation, Message, Role, ToolCall
from engine.domain.events import (
    AgentRunCompleted,
    AgentStepPaused,
    ChangesPublished,
    Event,
    HumanReviewCompleted,
    RunFailed,
    RunNamed,
    RunRequested,
    StepCompleted,
    StepReactivated,
    WorkspaceProvisioned,
)
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ApprovalId,
    ConversationId,
    MessageId,
    RunId,
    SessionGrantId,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.domain.state import RunPhase, RunState
from engine.domain.workflow import (
    AgentStep,
    HumanReviewStep,
    OutcomeTransition,
    StepOutput,
    TemplateBinding,
    TerminalOutcome,
    Transition,
    ValueReference,
    WorkflowDefinition,
    WorkflowTemplate,
    WorkspaceAccess,
    WorkspaceSpec,
)


class SQLiteStateStore:
    """Persist agent instances, runs, and conversations in one SQLite file.

    A single connection keeps ``:memory:`` useful in tests. SQLite calls are
    guarded because Streamlit may reuse a cached store from another thread.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_instances (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL UNIQUE,
                    task_id TEXT,
                    workspace_id TEXT,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    archived INTEGER NOT NULL DEFAULT 0,
                    runner TEXT NOT NULL DEFAULT '',
                    auto_approve INTEGER NOT NULL DEFAULT 0,
                    workflow_run_id TEXT,
                    workflow_step_id TEXT
                );

                CREATE TABLE IF NOT EXISTS run_states (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    state_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT NOT NULL,
                    tool_call_id TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    agent_run_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changed_files TEXT NOT NULL,
                    runner TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    approval_id TEXT NOT NULL UNIQUE,
                    agent_run_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    runner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT,
                    command TEXT,
                    cwd TEXT,
                    tool_name TEXT,
                    tool_call_id TEXT,
                    workspace_id TEXT,
                    arguments TEXT,
                    questions TEXT,
                    answers TEXT,
                    allowed_decisions TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    decision_source TEXT,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE INDEX IF NOT EXISTS approvals_by_run
                    ON approvals (agent_run_id);

                CREATE TABLE IF NOT EXISTS session_grants (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_id TEXT NOT NULL UNIQUE,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    runner TEXT NOT NULL,
                    approval_kind TEXT NOT NULL,
                    normalized_scope TEXT NOT NULL,
                    workspace_id TEXT,
                    created_from_approval_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS session_grants_by_instance
                    ON session_grants (instance_id);
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(agent_instances)"
                )
            }
            if "title" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN title TEXT NOT NULL DEFAULT 'New chat'"
                )
            if "archived" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )
            if "runner" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN runner TEXT NOT NULL DEFAULT ''"
                )
            if "auto_approve" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"
                )
            if "workflow_run_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances ADD COLUMN workflow_run_id TEXT"
                )
            if "workflow_step_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances ADD COLUMN workflow_step_id TEXT"
                )
            approval_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(approvals)")
            }
            if "workspace_id" not in approval_columns:
                # A database written before grants existed has approvals that
                # never recorded where they applied. Null is the honest value
                # for those: unknown, and therefore matching no grant.
                self._connection.execute(
                    "ALTER TABLE approvals ADD COLUMN workspace_id TEXT"
                )
            if "tool_call_id" not in approval_columns:
                # Approvals written before the pairing existed name no call, and
                # nothing can work out afterwards which one they were about.
                # Null reads as "unknown", which is what a client shows by
                # putting the request at the end of its turn rather than beside
                # a command it has guessed at.
                self._connection.execute(
                    "ALTER TABLE approvals ADD COLUMN tool_call_id TEXT"
                )
            if "questions" not in approval_columns:
                self._connection.execute(
                    "ALTER TABLE approvals ADD COLUMN questions TEXT"
                )
            if "answers" not in approval_columns:
                self._connection.execute(
                    "ALTER TABLE approvals ADD COLUMN answers TEXT"
                )

    # --- workflow runs ----------------------------------------------------

    async def load(self, run_id: RunId) -> RunState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM run_states WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _state_from_dict(json.loads(row["state_json"])) if row else None

    async def save(self, state: RunState) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO run_states (run_id, state_json) VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state_json = excluded.state_json
                """,
                (state.run_id, json.dumps(_state_to_dict(state))),
            )

    async def list_runs(self) -> Sequence[RunState]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, state_json FROM run_states ORDER BY sequence DESC"
            ).fetchall()
        runs: list[RunState] = []
        for row in rows:
            try:
                runs.append(_state_from_dict(json.loads(row["state_json"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                warnings.warn(
                    f"skipping incompatible workflow run {row['run_id']}: {error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return tuple(runs)

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        with self._lock, self._connection:
            self._connection.executemany(
                "INSERT INTO run_events (run_id, event_json) VALUES (?, ?)",
                ((run_id, json.dumps(_event_to_dict(event))) for event in events),
            )

    async def history(self, run_id: RunId) -> Sequence[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(_event_from_dict(json.loads(row["event_json"])) for row in rows)

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
        runner: str = "",
        *,
        instance_id: AgentInstanceId | None = None,
        conversation_id: ConversationId | None = None,
        workflow_run_id: RunId | None = None,
        workflow_step_id: StepId | None = None,
    ) -> AgentInstance:
        instance = AgentInstance(
            instance_id=instance_id or AgentInstanceId(f"agi-{uuid4().hex[:12]}"),
            agent_id=agent_id,
            conversation_id=conversation_id
            or ConversationId(f"conv-{uuid4().hex[:12]}"),
            task_id=task_id,
            workspace_id=workspace_id,
            runner=runner,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
        )
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?",
                (instance.instance_id,),
            ).fetchone()
            if existing is not None:
                loaded = await self.load_instance(instance.instance_id)
                assert loaded is not None
                return loaded
            self._connection.execute(
                """
                INSERT INTO agent_instances (
                    instance_id, agent_id, conversation_id, task_id,
                    workspace_id, runner, workflow_run_id, workflow_step_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.instance_id,
                    instance.agent_id,
                    instance.conversation_id,
                    instance.task_id,
                    instance.workspace_id,
                    instance.runner,
                    instance.workflow_run_id,
                    instance.workflow_step_id,
                ),
            )
        return instance

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
        auto_approve: bool = False,
    ) -> AgentInstance:
        with self._lock, self._connection:
            updated = self._connection.execute(
                """
                UPDATE agent_instances
                SET title = ?, archived = ?, runner = ?, auto_approve = ?
                WHERE instance_id = ?
                """,
                (title, archived, runner, auto_approve, instance_id),
            ).rowcount
            if not updated:
                raise KeyError(f"no agent instance {instance_id!r}")
        instance = await self.load_instance(instance_id)
        assert instance is not None
        return instance

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT instance_id, agent_id, conversation_id, task_id, workspace_id,
                       title, archived, runner, auto_approve,
                       workflow_run_id, workflow_step_id
                FROM agent_instances WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
        return _instance_from_row(row) if row is not None else None

    async def attach_workspace(
        self, instance_id: AgentInstanceId, workspace_id: WorkspaceId | None
    ) -> AgentInstance:
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE agent_instances SET workspace_id = ? WHERE instance_id = ?",
                (workspace_id, instance_id),
            ).rowcount
            if not updated:
                raise KeyError(f"no agent instance {instance_id!r}")
        instance = await self.load_instance(instance_id)
        assert instance is not None  # just updated, under the same lock
        return instance

    async def list_instances(
        self,
        agent_id: AgentId | None = None,
        *,
        workflow_run_id: RunId | None = None,
    ) -> Sequence[AgentInstance]:
        query = """
            SELECT instance_id, agent_id, conversation_id, task_id, workspace_id,
                   title, archived, runner, auto_approve,
                   workflow_run_id, workflow_step_id
            FROM agent_instances
        """
        filters: list[str] = []
        parameters: list[str] = []
        if agent_id is not None:
            filters.append("agent_id = ?")
            parameters.append(agent_id)
        if workflow_run_id is not None:
            filters.append("workflow_run_id = ?")
            parameters.append(workflow_run_id)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY sequence DESC"
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return tuple(_instance_from_row(row) for row in rows)

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        with self._lock:
            instance = self._connection.execute(
                "SELECT conversation_id FROM agent_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if instance is None:
                return None
            rows = self._connection.execute(
                """
                SELECT sequence, role, content, tool_calls, tool_call_id
                FROM messages WHERE instance_id = ? ORDER BY sequence
                """,
                (instance_id,),
            ).fetchall()
        return Conversation(
            conversation_id=ConversationId(instance["conversation_id"]),
            instance_id=instance_id,
            messages=tuple(_message_from_row(row) for row in rows),
        )

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"no agent instance {instance_id!r}")
            self._connection.executemany(
                """
                INSERT INTO messages (
                    instance_id, role, content, tool_calls, tool_call_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        instance_id,
                        message.role.value,
                        message.content,
                        json.dumps(
                            [
                                {
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "arguments": call.arguments,
                                }
                                for call in message.tool_calls
                            ]
                        ),
                        message.tool_call_id,
                    )
                    for message in messages
                ),
            )

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_runs (
                    agent_run_id, instance_id, status, summary, changed_files, runner
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_run_id) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    status = excluded.status,
                    summary = excluded.summary,
                    changed_files = excluded.changed_files,
                    runner = excluded.runner
                """,
                (
                    agent_run.agent_run_id,
                    agent_run.instance_id,
                    agent_run.status.value,
                    agent_run.summary,
                    json.dumps(agent_run.changed_files),
                    agent_run.runner,
                ),
            )

    # --- approvals --------------------------------------------------------

    async def record_approval(self, approval: ApprovalRecord) -> None:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?",
                (approval.instance_id,),
            ).fetchone()
            if exists is None:
                # The same integrity check `append_messages` makes, and stated
                # here rather than left to the foreign key so both stores fail
                # the same way.
                raise KeyError(f"no agent instance {approval.instance_id!r}")
            # Only the outcome is updatable: what was asked is a statement of
            # what the provider wanted at one moment and does not get revised.
            self._connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, agent_run_id, instance_id, runner, kind, reason,
                    command, cwd, tool_name, tool_call_id, workspace_id,
                    arguments, questions, answers, allowed_decisions, status,
                    decision, decision_source, requested_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status = excluded.status,
                    decision = excluded.decision,
                    decision_source = excluded.decision_source,
                    answers = excluded.answers,
                    decided_at = excluded.decided_at
                """,
                (
                    approval.approval_id,
                    approval.agent_run_id,
                    approval.instance_id,
                    approval.runner,
                    approval.kind.value,
                    approval.reason,
                    approval.command,
                    approval.cwd,
                    approval.tool_name,
                    approval.tool_call_id,
                    approval.workspace_id,
                    approval.arguments,
                    approval.questions,
                    approval.answers,
                    json.dumps(
                        [decision.value for decision in approval.allowed_decisions]
                    ),
                    approval.status.value,
                    approval.decision.value if approval.decision else None,
                    (
                        approval.decision_source.value
                        if approval.decision_source
                        else None
                    ),
                    approval.requested_at.isoformat(),
                    approval.decided_at.isoformat() if approval.decided_at else None,
                ),
            )

    async def load_approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        with self._lock:
            row = self._connection.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return _approval_from_row(row) if row is not None else None

    async def list_approvals(
        self,
        *,
        instance_id: AgentInstanceId | None = None,
        agent_run_id: AgentRunId | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[ApprovalRecord]:
        query = f"SELECT {_APPROVAL_COLUMNS} FROM approvals"
        filters: list[str] = []
        parameters: list[str] = []
        if instance_id is not None:
            filters.append("instance_id = ?")
            parameters.append(instance_id)
        if agent_run_id is not None:
            filters.append("agent_run_id = ?")
            parameters.append(agent_run_id)
        if status is not None:
            filters.append("status = ?")
            parameters.append(status.value)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY sequence"
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    async def record_session_grant(self, grant: SessionGrant) -> None:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?",
                (grant.instance_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"no agent instance {grant.instance_id!r}")
            # Only revocation is updatable: what was granted, to whom, and over
            # what is the statement the user made, and it does not get revised.
            self._connection.execute(
                """
                INSERT INTO session_grants (
                    grant_id, instance_id, runner, approval_kind, normalized_scope,
                    workspace_id, created_from_approval_id, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grant_id) DO UPDATE SET revoked_at = excluded.revoked_at
                """,
                (
                    grant.grant_id,
                    grant.instance_id,
                    grant.runner,
                    grant.approval_kind.value,
                    grant.normalized_scope,
                    grant.workspace_id,
                    grant.created_from_approval_id,
                    grant.created_at.isoformat(),
                    grant.revoked_at.isoformat() if grant.revoked_at else None,
                ),
            )

    async def list_session_grants(
        self, *, instance_id: AgentInstanceId | None = None
    ) -> Sequence[SessionGrant]:
        query = f"SELECT {_SESSION_GRANT_COLUMNS} FROM session_grants"
        parameters: tuple[str, ...] = ()
        if instance_id is not None:
            query += " WHERE instance_id = ?"
            parameters = (instance_id,)
        query += " ORDER BY sequence"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_session_grant_from_row(row) for row in rows)

    async def agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Read back one execution, matching the in-memory adapter's helper."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT agent_run_id, instance_id, status, summary, changed_files, runner
                FROM agent_runs WHERE agent_run_id = ?
                """,
                (agent_run_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentRun(
            agent_run_id=AgentRunId(row["agent_run_id"]),
            instance_id=AgentInstanceId(row["instance_id"]),
            status=AgentRunStatus(row["status"]),
            summary=row["summary"],
            changed_files=tuple(json.loads(row["changed_files"])),
            runner=row["runner"],
        )

    def close(self) -> None:
        """Release the underlying database connection."""
        with self._lock:
            self._connection.close()


def _instance_from_row(row: sqlite3.Row) -> AgentInstance:
    return AgentInstance(
        instance_id=AgentInstanceId(row["instance_id"]),
        agent_id=AgentId(row["agent_id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        task_id=TaskId(row["task_id"]) if row["task_id"] is not None else None,
        workspace_id=(
            WorkspaceId(row["workspace_id"]) if row["workspace_id"] is not None else None
        ),
        title=row["title"],
        archived=bool(row["archived"]),
        runner=row["runner"],
        auto_approve=bool(row["auto_approve"]),
        workflow_run_id=(
            RunId(row["workflow_run_id"])
            if row["workflow_run_id"] is not None
            else None
        ),
        workflow_step_id=(
            StepId(row["workflow_step_id"])
            if row["workflow_step_id"] is not None
            else None
        ),
    )


_APPROVAL_COLUMNS = """
    approval_id, agent_run_id, instance_id, runner, kind, reason, command, cwd,
    tool_name, tool_call_id, workspace_id, arguments, questions, answers,
    allowed_decisions, status, decision, decision_source, requested_at, decided_at
"""

_SESSION_GRANT_COLUMNS = """
    grant_id, instance_id, runner, approval_kind, normalized_scope, workspace_id,
    created_from_approval_id, created_at, revoked_at
"""


def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=ApprovalId(row["approval_id"]),
        agent_run_id=AgentRunId(row["agent_run_id"]),
        instance_id=AgentInstanceId(row["instance_id"]),
        runner=row["runner"],
        kind=ApprovalKind(row["kind"]),
        requested_at=datetime.fromisoformat(row["requested_at"]),
        reason=row["reason"],
        command=row["command"],
        cwd=row["cwd"],
        tool_name=row["tool_name"],
        tool_call_id=row["tool_call_id"],
        workspace_id=(
            WorkspaceId(row["workspace_id"]) if row["workspace_id"] is not None else None
        ),
        arguments=row["arguments"],
        questions=row["questions"],
        answers=row["answers"],
        allowed_decisions=tuple(
            ApprovalDecision(value) for value in json.loads(row["allowed_decisions"])
        ),
        status=ApprovalStatus(row["status"]),
        decision=(
            ApprovalDecision(row["decision"]) if row["decision"] is not None else None
        ),
        decision_source=(
            ApprovalDecisionSource(row["decision_source"])
            if row["decision_source"] is not None
            else None
        ),
        decided_at=(
            datetime.fromisoformat(row["decided_at"])
            if row["decided_at"] is not None
            else None
        ),
    )


def _session_grant_from_row(row: sqlite3.Row) -> SessionGrant:
    return SessionGrant(
        grant_id=SessionGrantId(row["grant_id"]),
        instance_id=AgentInstanceId(row["instance_id"]),
        runner=row["runner"],
        approval_kind=ApprovalKind(row["approval_kind"]),
        normalized_scope=row["normalized_scope"],
        created_from_approval_id=ApprovalId(row["created_from_approval_id"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        workspace_id=(
            WorkspaceId(row["workspace_id"]) if row["workspace_id"] is not None else None
        ),
        revoked_at=(
            datetime.fromisoformat(row["revoked_at"])
            if row["revoked_at"] is not None
            else None
        ),
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    calls = tuple(ToolCall(**call) for call in json.loads(row["tool_calls"]))
    return Message(
        role=Role(row["role"]),
        content=row["content"],
        tool_calls=calls,
        tool_call_id=row["tool_call_id"],
        message_id=MessageId(f"msg-{row['sequence']:06d}"),
    )


def _output_to_dict(output: StepOutput) -> dict[str, str]:
    return {"name": output.name, "value": output.value}


def _step_to_dict(step: StepCompleted) -> dict[str, object]:
    return {
        "run_id": step.run_id,
        "step_id": step.step_id,
        "agent_run_id": step.agent_run_id,
        "outcome": step.outcome,
        "summary": step.summary,
        "outputs": [_output_to_dict(output) for output in step.outputs],
        "mcp_request_id": step.mcp_request_id,
    }


def _step_from_dict(value: dict[str, object]) -> StepCompleted:
    return StepCompleted(
        run_id=RunId(str(value["run_id"])),
        step_id=StepId(str(value["step_id"])),
        agent_run_id=AgentRunId(str(value["agent_run_id"])),
        outcome=str(value["outcome"]),
        summary=str(value["summary"]),
        outputs=tuple(
            StepOutput(name=str(output["name"]), value=str(output["value"]))
            for output in value.get("outputs", [])
            if isinstance(output, dict)
        ),
        mcp_request_id=value.get("mcp_request_id"),
    )


def _review_to_dict(review: HumanReviewCompleted) -> dict[str, object]:
    return {
        "run_id": review.run_id,
        "step_id": review.step_id,
        "approved": review.approved,
        "summary": review.summary,
    }


def _review_from_dict(value: dict[str, object]) -> HumanReviewCompleted:
    return HumanReviewCompleted(
        run_id=RunId(str(value["run_id"])),
        step_id=StepId(str(value["step_id"])),
        approved=bool(value["approved"]),
        summary=str(value.get("summary", "")),
    )


def _state_to_dict(state: RunState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "workflow_id": state.workflow_id,
        "phase": state.phase.value,
        "repository": state.repository,
        "prompt": state.prompt,
        "name": state.name,
        "workspace_id": state.workspace_id,
        "agent_runs": list(state.agent_runs),
        "max_agent_runs": state.max_agent_runs,
        "current_step_id": state.current_step_id,
        "current_agent_run_id": state.current_agent_run_id,
        "agent_paused": state.agent_paused,
        "runner_name": state.runner_name,
        "step_results": [_step_to_dict(step) for step in state.step_results],
        "human_review": (
            _review_to_dict(state.human_review) if state.human_review else None
        ),
        "human_reviews": [
            _review_to_dict(review) for review in state.human_reviews
        ],
        "failure_reason": state.failure_reason,
        "workflow_definition": (
            _workflow_to_dict(state.workflow_definition)
            if state.workflow_definition is not None
            else None
        ),
    }


def _state_from_dict(value: dict[str, object]) -> RunState:
    review = value.get("human_review")
    raw_reviews = value.get("human_reviews")
    reviews = (
        tuple(
            _review_from_dict(item)
            for item in raw_reviews
            if isinstance(item, dict)
        )
        if isinstance(raw_reviews, list)
        else (_review_from_dict(review),)
        if isinstance(review, dict)
        else ()
    )
    return RunState(
        run_id=RunId(str(value["run_id"])),
        task_id=TaskId(str(value["task_id"])),
        workflow_id=WorkflowId(str(value["workflow_id"])),
        phase=RunPhase(str(value["phase"])),
        repository=str(value.get("repository", "")),
        prompt=str(value.get("prompt", "")),
        name=str(value.get("name", "")),
        workspace_id=(
            WorkspaceId(str(value["workspace_id"]))
            if value.get("workspace_id") is not None
            else None
        ),
        agent_runs=tuple(
            AgentRunId(str(agent_run_id))
            for agent_run_id in value.get("agent_runs", [])
        ),
        max_agent_runs=int(value.get("max_agent_runs", 3)),
        current_step_id=(
            StepId(str(value["current_step_id"]))
            if value.get("current_step_id") is not None
            else None
        ),
        current_agent_run_id=(
            AgentRunId(str(value["current_agent_run_id"]))
            if value.get("current_agent_run_id") is not None
            else None
        ),
        agent_paused=bool(value.get("agent_paused", False)),
        runner_name=str(value.get("runner_name", "")),
        step_results=tuple(
            _step_from_dict(step)
            for step in value.get("step_results", [])
            if isinstance(step, dict)
        ),
        human_review=(
            _review_from_dict(review) if isinstance(review, dict) else None
        ),
        human_reviews=reviews,
        failure_reason=str(value.get("failure_reason", "")),
        workflow_definition=(
            _workflow_from_dict(value["workflow_definition"])
            if isinstance(value.get("workflow_definition"), dict)
            else None
        ),
    )


def _profile_to_dict(profile: AgentProfile) -> dict[str, object]:
    return {
        "agent_id": profile.agent_id,
        "instructions": profile.instructions,
        "capabilities": list(profile.capabilities),
        "model": profile.model,
        "description": profile.description,
        "read_only": profile.read_only,
    }


def _profile_from_dict(value: dict[str, object]) -> AgentProfile:
    return AgentProfile(
        agent_id=AgentId(str(value["agent_id"])),
        instructions=str(value["instructions"]),
        capabilities=tuple(str(item) for item in value.get("capabilities", [])),
        model=str(value.get("model", "")),
        description=str(value.get("description", "")),
        read_only=bool(value.get("read_only", False)),
    )


def _template_to_dict(template: WorkflowTemplate) -> dict[str, object]:
    return {
        "text": template.text,
        "bindings": [
            {
                "name": binding.name,
                "source": binding.reference.source,
                "step_id": binding.reference.step_id,
                "field": binding.reference.field,
            }
            for binding in template.bindings
        ],
    }


def _template_from_dict(value: dict[str, object]) -> WorkflowTemplate:
    return WorkflowTemplate(
        text=str(value["text"]),
        bindings=tuple(
            TemplateBinding(
                name=str(binding["name"]),
                reference=ValueReference(
                    source=str(binding["source"]),
                    step_id=(
                        StepId(str(binding["step_id"]))
                        if binding.get("step_id") is not None
                        else None
                    ),
                    field=str(binding.get("field", "")),
                ),
            )
            for binding in value.get("bindings", [])
            if isinstance(binding, dict)
        ),
    )


def _transition_to_dict(transition: Transition) -> dict[str, object]:
    return {
        "step_id": transition.step_id,
        "terminal": transition.terminal.value if transition.terminal else None,
    }


def _transition_from_dict(value: dict[str, object]) -> Transition:
    return Transition(
        step_id=(StepId(str(value["step_id"])) if value.get("step_id") else None),
        terminal=(
            TerminalOutcome(str(value["terminal"]))
            if value.get("terminal")
            else None
        ),
    )


def _workflow_to_dict(definition: WorkflowDefinition) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for step in definition.steps:
        if isinstance(step, AgentStep):
            steps.append(
                {
                    "kind": "agent",
                    "step_id": step.step_id,
                    "name": step.name,
                    "profile": _profile_to_dict(step.profile),
                    "prompt": _template_to_dict(step.prompt),
                    "transitions": [
                        {
                            "outcome": edge.outcome,
                            "transition": _transition_to_dict(edge.transition),
                        }
                        for edge in step.transitions
                    ],
                    "required_outputs": list(step.required_outputs),
                    "editable": step.editable,
                    "workspace_access": step.workspace_access.value,
                }
            )
        else:
            steps.append(
                {
                    "kind": "human_review",
                    "step_id": step.step_id,
                    "name": step.name,
                    "title": _template_to_dict(step.title),
                    "summary": _template_to_dict(step.summary),
                    "approved": _transition_to_dict(step.approved),
                    "rejected": _transition_to_dict(step.rejected),
                }
            )
    return {
        "workflow_id": definition.workflow_id,
        "name": definition.name,
        "version": definition.version,
        "workspace": {"base_ref": definition.workspace.base_ref},
        "steps": steps,
        "naming_profile": (
            _profile_to_dict(definition.naming_profile)
            if definition.naming_profile is not None
            else None
        ),
        "naming_prompt": definition.naming_prompt,
    }


def _workflow_from_dict(value: dict[str, object]) -> WorkflowDefinition:
    steps = []
    for raw in value.get("steps", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("kind") == "agent":
            steps.append(
                AgentStep(
                    step_id=StepId(str(raw["step_id"])),
                    name=str(raw["name"]),
                    profile=_profile_from_dict(raw["profile"]),
                    prompt=_template_from_dict(raw["prompt"]),
                    transitions=tuple(
                        OutcomeTransition(
                            outcome=str(edge["outcome"]),
                            transition=_transition_from_dict(edge["transition"]),
                        )
                        for edge in raw.get("transitions", [])
                        if isinstance(edge, dict)
                    ),
                    required_outputs=tuple(
                        str(item) for item in raw.get("required_outputs", [])
                    ),
                    editable=bool(raw.get("editable", False)),
                    workspace_access=WorkspaceAccess(
                        str(raw.get("workspace_access", "read"))
                    ),
                )
            )
        else:
            steps.append(
                HumanReviewStep(
                    step_id=StepId(str(raw["step_id"])),
                    name=str(raw["name"]),
                    title=_template_from_dict(raw["title"]),
                    summary=_template_from_dict(raw["summary"]),
                    approved=_transition_from_dict(raw["approved"]),
                    rejected=_transition_from_dict(raw["rejected"]),
                )
            )
    workspace = value.get("workspace", {})
    naming = value.get("naming_profile")
    return WorkflowDefinition(
        workflow_id=WorkflowId(str(value["workflow_id"])),
        name=str(value["name"]),
        version=str(value["version"]),
        steps=tuple(steps),
        workspace=WorkspaceSpec(
            base_ref=str(workspace.get("base_ref", "origin/main"))
            if isinstance(workspace, dict)
            else "origin/main"
        ),
        naming_profile=(
            _profile_from_dict(naming) if isinstance(naming, dict) else None
        ),
        naming_prompt=str(value.get("naming_prompt", "")),
    )


def _event_to_dict(event: Event) -> dict[str, object]:
    if isinstance(event, StepCompleted):
        return {"type": "StepCompleted", **_step_to_dict(event)}
    if isinstance(event, AgentStepPaused):
        return {
            "type": "AgentStepPaused",
            "run_id": event.run_id,
            "step_id": event.step_id,
            "agent_run_id": event.agent_run_id,
        }
    if isinstance(event, StepReactivated):
        return {
            "type": "StepReactivated",
            "run_id": event.run_id,
            "step_id": event.step_id,
        }
    if isinstance(event, HumanReviewCompleted):
        return {"type": "HumanReviewCompleted", **_review_to_dict(event)}
    if isinstance(event, RunRequested):
        return {
            "type": "RunRequested",
            "run_id": event.run_id,
            "task_id": event.task_id,
            "prompt": event.prompt,
            "repository": event.repository,
            "workflow_id": event.workflow_id,
        }
    if isinstance(event, RunNamed):
        return {
            "type": "RunNamed",
            "run_id": event.run_id,
            "name": event.name,
        }
    if isinstance(event, WorkspaceProvisioned):
        return {
            "type": "WorkspaceProvisioned",
            "run_id": event.run_id,
            "workspace_id": event.workspace_id,
            "root_path": event.root_path,
        }
    if isinstance(event, AgentRunCompleted):
        return {
            "type": "AgentRunCompleted",
            "run_id": event.run_id,
            "agent_run_id": event.agent_run_id,
            "succeeded": event.succeeded,
            "summary": event.summary,
            "changed_files": list(event.changed_files),
        }
    if isinstance(event, ChangesPublished):
        return {
            "type": "ChangesPublished",
            "run_id": event.run_id,
            "review_url": event.review_url,
        }
    if isinstance(event, RunFailed):
        return {
            "type": "RunFailed",
            "run_id": event.run_id,
            "reason": event.reason,
            "agent_run_id": event.agent_run_id,
            "mcp_request_id": event.mcp_request_id,
        }
    raise TypeError(f"cannot persist event {type(event).__name__}")


def _event_from_dict(value: dict[str, object]) -> Event:
    kind = value["type"]
    if kind == "StepCompleted":
        return _step_from_dict(value)
    if kind == "AgentStepPaused":
        return AgentStepPaused(
            run_id=RunId(str(value["run_id"])),
            step_id=StepId(str(value["step_id"])),
            agent_run_id=AgentRunId(str(value["agent_run_id"])),
        )
    if kind == "StepReactivated":
        return StepReactivated(
            run_id=RunId(str(value["run_id"])),
            step_id=StepId(str(value["step_id"])),
        )
    if kind == "HumanReviewCompleted":
        return _review_from_dict(value)
    if kind == "RunRequested":
        return RunRequested(
            run_id=RunId(str(value["run_id"])),
            task_id=TaskId(str(value["task_id"])),
            prompt=str(value["prompt"]),
            repository=str(value["repository"]),
            workflow_id=WorkflowId(str(value["workflow_id"])),
        )
    if kind == "RunNamed":
        return RunNamed(
            run_id=RunId(str(value["run_id"])),
            name=str(value["name"]),
        )
    if kind == "WorkspaceProvisioned":
        return WorkspaceProvisioned(
            run_id=RunId(str(value["run_id"])),
            workspace_id=WorkspaceId(str(value["workspace_id"])),
            root_path=str(value["root_path"]),
        )
    if kind == "AgentRunCompleted":
        return AgentRunCompleted(
            run_id=RunId(str(value["run_id"])),
            agent_run_id=AgentRunId(str(value["agent_run_id"])),
            succeeded=bool(value["succeeded"]),
            summary=str(value["summary"]),
            changed_files=tuple(str(path) for path in value.get("changed_files", [])),
        )
    if kind == "ChangesPublished":
        return ChangesPublished(
            run_id=RunId(str(value["run_id"])),
            review_url=str(value["review_url"]),
        )
    if kind == "RunFailed":
        return RunFailed(
            run_id=RunId(str(value["run_id"])),
            reason=str(value["reason"]),
            agent_run_id=(
                AgentRunId(str(value["agent_run_id"]))
                if value.get("agent_run_id") is not None
                else None
            ),
            mcp_request_id=value.get("mcp_request_id"),
        )
    raise ValueError(f"unknown persisted event type {kind!r}")


__all__ = ["SQLiteStateStore"]
