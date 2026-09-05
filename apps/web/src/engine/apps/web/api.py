"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Their tasks are owned by the
service rather than by one response, so a refreshed browser can reconnect.
A lock per thread prevents two turns from reading the same stale transcript.

Approvals have their own replayable event feed. Their durable record is loaded
when a browser subscribes, while process-local notifications wake that feed for
later transitions without polling the transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Container,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from engine.apps.web import source_control as source_control_settings
from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowState,
    GitHubAuthError,
    GitHubCredentialStore,
    credentials_from_device_flow,
    poll_device_flow,
    start_device_flow,
)
from engine.apps.web.source_control import (
    SourceControlPreferences,
)
from engine.adapters.communications.slack import (
    SlackAuthError,
    SlackCredentialStore,
    SlackMention,
    authorization_url as slack_authorization_url,
    exchange_code as exchange_slack_code,
    mention_from_event as slack_mention_from_event,
    revoke_token as revoke_slack_token,
    verify_signature as verify_slack_signature,
)
from engine.domain import (
    AgentId,
    AgentInstance,
    AgentInstanceId,
    AgentRunId,
    AgentRunStatus,
    AgentStep,
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    HumanReviewCompleted,
    Message,
    Milestone,
    MilestoneId,
    Project,
    ProjectId,
    Role,
    RunId,
    RunOrigin,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepId,
    TaskId,
    WorkflowDefinition,
    WorkflowId,
    WorkspaceId,
    Workstream,
    WorkstreamId,
    instance_id_for_project,
    project_id_for_instance,
    workstreams_by_milestone,
)
from engine.graph_runtime import (
    EventKind,
    EventLog,
    GraphCompilationError,
    GraphId,
    GraphRuntime,
    GraphRuntimeError,
    GraphWorkflow,
    RunStatus,
    RuntimeEvent,
)
from engine.graph_runtime import create_app as create_graph_app
from engine.ports import (
    AgentRunner,
    ApprovalHandler,
    InteractiveAgentRunner,
    Message as CommunicationsMessage,
    StateStore,
    UserInputAnswer,
    WorkspaceState,
)
from engine.runtime import (
    PLANNER,
    AgentSession,
    ApprovalBroker,
    ApprovalConfig,
    ApprovalDecisionNotAllowedError,
    ApprovalNotPendingError,
    RunNotifier,
    RunReader,
    UnknownApprovalError,
    UserInputNotAllowedError,
    WorkflowCatalog,
    WorkflowExecutionError,
    WorkflowExecutor,
    WorkflowRunView,
    WorkOrdersConfig,
    load_engine_config,
    load_workflow_catalog,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send


@dataclass(slots=True)
class ChatThread:
    """UI metadata for one engine agent instance."""

    instance_id: AgentInstanceId
    agent_id: AgentId
    runner: str
    title: str = "New chat"
    archived: bool = False
    workspace_root: str | None = None
    workspace_id: WorkspaceId | None = None
    workspace_ref: str | None = None
    """What to check out to read this chat's work, checkout or no checkout."""
    workflow_run_id: RunId | None = None
    workflow_step_id: StepId | None = None
    editable: bool = False
    """Whether this workflow step permits human messages and interruption."""
    auto_approve: bool = False
    """Whether system auto-approvals are enabled for this conversation."""


class ActiveRun:
    """One agent turn whose lifetime is independent of an HTTP connection.

    Subscribers receive complete content snapshots, so a browser that refreshes
    can reconnect without needing to know which individual events it missed. An
    approval is the same idea and for the same reason: the turn is paused on a
    question, and a subscriber that arrives after it was asked has to be told
    the question rather than left watching a stream that has gone quiet.
    """

    def __init__(
        self,
        agent_run_id: AgentRunId,
        known_tool_call_ids: Iterable[str] = (),
    ) -> None:
        self.agent_run_id = agent_run_id
        self.content: list[dict[str, object]] = []
        # assistant-ui registers tool calls as resources by id and throws when
        # one occurs twice. Providers can replay an already completed item, so
        # keep the ids from earlier turns as well as the parts in this one.
        self._tool_calls: dict[str, dict[str, object]] = {
            call_id: {} for call_id in known_tool_call_ids
        }
        self.approvals: dict[str, dict[str, object]] = {}
        """The latest snapshot of every request this run has raised, by id.

        A map rather than "the one the turn is on", because what a subscriber
        needs is each request's *transition*: a turn let go by a decision often
        asks its next question before anyone has been told about the answer, and
        a single slot would hand the new question over in place of it -- leaving
        a card waiting forever on a request that was decided.
        """
        self._approval_transitions: list[dict[str, object]] = []
        """Every distinct state the run stream must deliver, in order.

        The latest-state map makes reconnect snapshots cheap, but cannot serve
        as an event queue: a pending request may become decided before the
        stream task next runs. Keeping the transitions separately makes stream
        delivery independent of event-loop scheduling.
        """
        self.error: str | None = None
        self.done = False
        self._revision = 0
        self._changed = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None

    def start(self, say: Awaitable[str]) -> None:
        self._task = asyncio.create_task(self._run(say))

    async def cancel(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def stream(self) -> AsyncIterator[bytes]:
        revision = 0
        transition_index = 0
        sent: dict[str, dict[str, object]] = {}
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                transitions = [
                    dict(value)
                    for value in self._approval_transitions[transition_index:]
                ]
                transition_index += len(transitions)
                error = self.error
                done = self.done

            for approval in transitions:
                approval_id = str(approval["id"])
                # Whole snapshots, including the resolved ones: a client that
                # missed the decision would otherwise go on showing a prompt
                # for a request that has already been answered. Every one that
                # has moved rather than only the newest, because several can
                # move between two wakes and the one being answered is exactly
                # the one that would be dropped. Emitted before the terminal
                # events so the last thing said about a request is never lost to
                # the run ending in the same breath.
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _json_line({"type": "approval", "approval": approval})
            if error is not None:
                yield _json_line({"type": "error", "error": error})
                return
            if done:
                yield _json_line({"type": "done", "content": content})
                return
            yield _json_line({"type": "content", "content": content})

    async def _run(self, say: Awaitable[str]) -> None:
        try:
            answer = await say
            if answer and not any(
                part.get("type") == "text" and part.get("text") == answer
                for part in self.content
            ):
                self.content.append({"type": "text", "text": answer})
            await self._finish()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            await self._finish()
        except asyncio.CancelledError:
            await self._finish()
            raise

    async def observe(self, message: Message) -> None:
        if not _merge_message(self.content, message, self._tool_calls):
            return
        async with self._changed:
            self._revision += 1
            self._changed.notify_all()

    async def present_approval(self, approval: ApprovalRecord) -> None:
        """Publish what the turn is waiting on, and wake the subscribers.

        For a pause: nothing else is going to happen on this run until somebody
        answers, so this is the only thing that will wake anyone.
        """
        snapshot = _approval_json(approval)
        async with self._changed:
            self.approvals[str(approval.approval_id)] = snapshot
            self._approval_transitions.append(snapshot)
            self._revision += 1
            self._changed.notify_all()

    def note_approval(self, approval: ApprovalRecord) -> None:
        """Update the snapshot without waking anyone, for a run that is ending.

        Synchronous on purpose. The wake that matters is the one the run's own
        ending sends a moment later, and awaiting a lock here would yield the
        event loop back to the very turn being torn down.
        """
        snapshot = _approval_json(approval)
        self.approvals[str(approval.approval_id)] = snapshot
        self._approval_transitions.append(snapshot)

    async def _finish(self) -> None:
        async with self._changed:
            self.done = True
            self._revision += 1
            self._changed.notify_all()


class ApprovalFeed:
    """Replay durable approval snapshots, then push each later transition.

    Persistence remains the source of truth. The condition is only a wake-up
    signal, so reconnecting after a lost HTTP connection cannot lose an event.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._revisions: dict[AgentInstanceId, int] = {}
        self._changed: dict[AgentInstanceId, asyncio.Condition] = {}

    async def publish(self, approval: ApprovalRecord) -> None:
        condition = self._changed.setdefault(approval.instance_id, asyncio.Condition())
        async with condition:
            self._revisions[approval.instance_id] = (
                self._revisions.get(approval.instance_id, 0) + 1
            )
            condition.notify_all()

    async def stream(self, instance_id: AgentInstanceId) -> AsyncIterator[bytes]:
        condition = self._changed.setdefault(instance_id, asyncio.Condition())
        sent: dict[str, dict[str, object]] = {}
        # Flush the response immediately even when this conversation has never
        # asked for approval. EventSource ignores comment frames.
        yield b": connected\n\n"
        while True:
            revision = self._revisions.get(instance_id, 0)
            approvals = await self._store.list_approvals(instance_id=instance_id)
            for record in approvals:
                approval = _approval_json(record)
                approval_id = str(record.approval_id)
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _server_event(approval)

            async with condition:
                await condition.wait_for(
                    lambda: self._revisions.get(instance_id, 0) > revision
                )


class BuiltClient(StaticFiles):
    """The Vite build, cached the way its filenames say it should be.

    Asset names carry a content hash, so those files are safe to keep forever
    and are never the reason a browser is out of date. The page that *names*
    them is the opposite: served without instructions, browsers cache it
    heuristically and go on asking for the hashed files of a build that no
    longer exists, which arrives as a blank page and a pair of 404s. So the
    entry point is revalidated every time and the hashed assets are not.
    """

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = Path(full_path).parent.name == "assets"
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


class ThreadService:
    """Coordinates assistant-ui threads over an ``AgentSession``."""

    def __init__(
        self,
        session: AgentSession,
        runners: Mapping[str, AgentRunner],
        workflow_catalog: WorkflowCatalog | None = None,
        approval_policy: ApprovalConfig = ApprovalConfig(),
        *,
        approval_observer: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.approvals = ApprovalBroker(
            session.state_store, approval_policy, observe=approval_observer
        )
        """Public alongside `session`: the same durable boundary, for pauses."""
        self._runners = runners
        self._workflow_catalog = workflow_catalog
        self._threads: dict[AgentInstanceId, ChatThread] = {}
        self._locks: dict[AgentInstanceId, asyncio.Lock] = {}
        self._active_runs: dict[AgentInstanceId, ActiveRun] = {}
        self._restored = False
        self._restore_lock = asyncio.Lock()

    async def list(self) -> tuple[ChatThread, ...]:
        await self._restore()
        return tuple(
            thread
            for thread in reversed(self._threads.values())
            if thread.workflow_run_id is None
        )

    async def get(self, instance_id: AgentInstanceId) -> ChatThread | None:
        await self._restore()
        thread = self._threads.get(instance_id)
        if thread is not None:
            return thread
        # Workflow workers may materialize a step after this web process has
        # restored its initial registry. Resolve direct conversation links from
        # the durable store instead of requiring a server restart.
        instance = await self.session.instance(instance_id)
        if instance is None:
            return None
        thread = ChatThread(
            instance.instance_id,
            instance.agent_id,
            (
                instance.runner
                if instance.runner in self.session.runners
                else self.session.default_runner
            ),
            title=await self._instance_title(instance),
            archived=instance.archived,
            workflow_run_id=instance.workflow_run_id,
            workflow_step_id=instance.workflow_step_id,
            editable=await self._instance_step_editable(instance),
            auto_approve=instance.auto_approve,
        )
        self._threads[instance.instance_id] = await self._sync_workspace(thread)
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def create(self, agent_id: AgentId, runner: str) -> ChatThread:
        await self._restore()
        if runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        instance = await self.session.start(agent_id, runner=runner)
        thread = ChatThread(instance.instance_id, agent_id, runner)
        await self._sync_workspace(thread)
        self._threads[instance.instance_id] = thread
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def attach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Give this chat a checkout again -- or a first one."""
        thread = await self._require_idle(instance_id)
        repository = None
        base_ref = None
        if thread.workflow_run_id is not None:
            run = await self.session.state_store.load(thread.workflow_run_id)
            if run is None:
                raise RuntimeError("WorkOrder not found")
            repository = run.repository
            definition = run.workflow_definition
            if definition is None and self._workflow_catalog is not None:
                definition = self._workflow_catalog.get(run.workflow_id)
            base_ref = definition.workspace.base_ref if definition is not None else None
        async with self._locks[instance_id]:
            state = await self.session.attach_workspace(
                instance_id, repository=repository, base_ref=base_ref
            )
        return self._apply_workspace_state(thread, state)

    async def detach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Release this chat's checkout, keeping its work on the branch."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.detach_workspace(instance_id)
        return self._apply_workspace_state(thread, state)

    def _apply_workspace_state(
        self, thread: ChatThread, state: WorkspaceState | None
    ) -> ChatThread:
        """Refresh every loaded conversation sharing the changed workspace."""
        workspace_id = state.workspace_id if state is not None else thread.workspace_id
        if workspace_id is not None:
            for cached in self._threads.values():
                if cached.workspace_id == workspace_id:
                    _with_workspace(cached, state)
        return _with_workspace(thread, state)

    async def _require_idle(self, instance_id: AgentInstanceId) -> ChatThread:
        """A workspace is not the agent's to lose in the middle of using it.

        The turn lock alone would serialize this correctly but leave the
        request hanging for as long as the agent runs, which reads as a broken
        button rather than a busy one.
        """
        thread = await self._require(instance_id)
        if self.active_run(instance_id) is not None:
            raise RuntimeError("this chat has a run in progress")
        return thread

    async def delete(self, instance_id: AgentInstanceId) -> None:
        await self._restore()
        self._threads.pop(instance_id, None)
        self._locks.pop(instance_id, None)

    async def history(self, instance_id: AgentInstanceId) -> tuple[Message, ...]:
        await self._require(instance_id)
        return await self.session.history(instance_id)

    async def say(
        self,
        instance_id: AgentInstanceId,
        text: str,
        runner: str | None,
        observed: asyncio.Queue[Message],
        on_approval: ApprovalHandler | None = None,
        agent_run_id: AgentRunId | None = None,
    ) -> str:
        thread = await self._require(instance_id)
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        thread.runner = selected_runner
        await self._persist_metadata(thread)

        async with self._locks[instance_id]:
            turn = await self.session.say(
                instance_id,
                text,
                runner=selected_runner,
                on_message=observed.put_nowait,
                on_approval=on_approval,
                agent_run_id=agent_run_id,
            )
        return turn.message.content

    async def start_run(
        self, instance_id: AgentInstanceId, text: str, runner: str | None
    ) -> ActiveRun:
        thread = await self._require(instance_id)
        await self.require_somewhere_to_run(instance_id)
        history = await self.session.history(instance_id)
        initial_message_count = len(history)
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        # Named before it starts, because the approvals it raises are brokered
        # against this run and a decision has to be able to name it too.
        agent_run_id = _new_agent_run_id()
        selected_runner = runner or thread.runner
        run = ActiveRun(agent_run_id, _tool_call_ids(history))
        self._active_runs[instance_id] = run
        on_approval = None
        # The runner the session will hand this turn to, not the one the name
        # alone would pick: an agent that only reads is answered by a different
        # object, and both whether it can pause and how it reads its own
        # requests are that object's to say. An unknown name or an agent this
        # process no longer composes leaves this unresolved, and the turn then
        # fails in the session exactly where it failed before.
        profile = self.session.profiles.get(thread.agent_id)
        selected = (
            self.session.runner_for(thread.agent_id, selected_runner)
            if profile is not None and selected_runner in self.session.runners
            else None
        )
        if profile is not None and isinstance(selected, InteractiveAgentRunner):
            on_approval = self.approvals.handler(
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                runner=selected_runner,
                present=run.present_approval,
                # Where this turn will actually work, so consent it collects is
                # bounded by the same worktree the agent is standing in.
                workspace_id=thread.workspace_id,
                # How this provider's requests read as Engine capabilities, so
                # the configured policy has something to evaluate them against.
                translator=selected.permission_translator,
                # And what this agent is, which the policy does not get to
                # widen: a planner asking to edit is refused here, whatever the
                # deployment allows a coder.
                read_only=profile.read_only,
            )

        async def execute() -> str:
            task = asyncio.create_task(
                self.say(
                    instance_id,
                    text,
                    selected_runner,
                    observed,
                    on_approval,
                    agent_run_id,
                )
            )
            try:
                while not task.done() or not observed.empty():
                    try:
                        async with asyncio.timeout(0.1):
                            message = await observed.get()
                    except TimeoutError:
                        continue
                    await run.observe(message)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if on_approval is not None:
                    # However this turn ended, nothing is waiting on its
                    # requests any more -- a provider that died mid-question
                    # leaves one here. Every one of them, because a client is
                    # showing whichever it was last told about, and any of those
                    # has to stop saying "pending", whoever resolved it.
                    await self.approvals.interrupt_run(agent_run_id)
                    for asked in await self.session.state_store.list_approvals(
                        agent_run_id=agent_run_id
                    ):
                        run.note_approval(asked)

        run.start(execute())
        # Ensure a refresh can load the submitted question before this POST
        # starts returning streamed response bytes.
        while (
            len(await self.session.history(instance_id)) <= initial_message_count
            and not run.done
        ):
            await asyncio.sleep(0)
        return run

    def active_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        run = self._active_runs.get(instance_id)
        return run if run is not None and not run.done else None

    def latest_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        """The latest run, including a just-finished run needed by a racing resume."""
        return self._active_runs.get(instance_id)

    def auto_approve_enabled(self, instance_id: AgentInstanceId) -> bool:
        """The live per-conversation override read by workflow approval handlers."""

        thread = self._threads.get(instance_id)
        return bool(thread and thread.auto_approve)

    async def decide_approval(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        decision: str,
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer what this chat's current run is paused on.

        Scoped to the run rather than the conversation: an id from a turn that
        has already ended names a provider process nobody can resume, and
        applying its answer to whatever is running now would approve a command
        the user never saw.
        """
        await self._require(instance_id)
        run = self.active_run(instance_id)
        try:
            chosen = ApprovalDecision(decision)
        except ValueError:
            raise ApprovalDecisionNotAllowedError(
                f"unknown decision {decision!r}"
            ) from None
        record = await self.approvals.decide(
            approval_id,
            chosen,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def answer_question(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        answers: tuple[UserInputAnswer, ...],
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer a structured prompt from this chat's current run."""

        await self._require(instance_id)
        run = self.active_run(instance_id)
        record = await self.approvals.answer(
            approval_id,
            answers,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def stop_run(self, instance_id: AgentInstanceId) -> None:
        """Stop this chat's run, whether it is working or waiting on a person.

        What it was waiting on is resolved as a cancellation before the turn is
        torn down, so the answer to "was that command allowed?" is a recorded
        no rather than a row that stops mid-sentence. Tearing the turn down
        then does the rest: cancelling one request would not oblige the agent
        to stop asking, and stopping means stopping.
        """
        run = self.active_run(instance_id)
        if run is None:
            return
        for resolved in await self.approvals.cancel_run(run.agent_run_id):
            run.note_approval(resolved)
        await run.cancel()
        await self._record_cancelled(run.agent_run_id)

    async def _record_cancelled(self, agent_run_id: AgentRunId) -> None:
        """Record the stopped run as a cancellation, however the turn ended.

        A cancelled approval is a decision the provider can act on, so a
        well-behaved one answers it by tidying up and returning -- and a turn
        that returns is a turn the session records as a success. Left there,
        stopping a paused run would read afterwards as one that finished
        normally. Whatever the provider made of the last second, the user
        withdrew this turn.
        """
        store = self.session.state_store
        agent_run = await store.agent_run(agent_run_id)
        if agent_run is None or agent_run.status is AgentRunStatus.CANCELLED:
            return
        await store.record_agent_run(
            replace(agent_run, status=AgentRunStatus.CANCELLED, summary="cancelled")
        )

    async def generate_title(
        self,
        instance_id: AgentInstanceId,
        opening_text: str | None = None,
        runner: str | None = None,
    ) -> str:
        """Ask the thread's agent for a title without changing its transcript."""
        thread = await self._require(instance_id)
        project_id = project_id_for_instance(instance_id)
        project = await self.session.state_store.load_project(project_id)
        names_project = thread.title == "New project" and project is not None
        if thread.title != "New chat" and not names_project:
            return thread.title
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        async with self._locks[instance_id]:
            if thread.title not in {"New chat", "New project"}:
                return thread.title
            history = await self.session.history(instance_id)
            title_context = (
                (*history, Message.user(opening_text)) if opening_text else history
            )
            # The session's answer rather than the name's, so a chat with an
            # agent that only reads is named by a runner that only reads too.
            turn = await self.session.runner_for(
                thread.agent_id, selected_runner
            ).run_turn(
                _new_agent_run_id(),
                self.session.profiles[thread.agent_id],
                (*title_context, Message.user(_TITLE_PROMPT)),
                # Naming a chat reads the transcript, not the tree, so a
                # detached one is named where the process runs rather than
                # failing on a directory it does not need.
                workspace_id=thread.workspace_id if thread.workspace_root else None,
            )
        title = _clean_title(turn.message.content)
        if title:
            # Project creation is part of thread initialization, before the
            # client receives the permalink. Rename that durable placeholder
            # before consuming the sentinel title so an interrupted request is
            # always safe to retry after a reload.
            if project is not None and names_project:
                # Renamed rather than rewritten, so a project put away while it
                # was still being named comes back still put away.
                await self.session.state_store.save_project(
                    replace(project, name=title)
                )
            thread.title = title
            await self._persist_metadata(thread)
        return thread.title

    async def update_metadata(
        self,
        instance_id: AgentInstanceId,
        *,
        title: str | None = None,
        runner: str | None = None,
        archived: bool | None = None,
        auto_approve: bool | None = None,
    ) -> ChatThread:
        thread = await self._require(instance_id)
        if runner is not None and runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        if title is not None:
            thread.title = title
        if runner is not None:
            thread.runner = runner
        if archived is not None:
            thread.archived = archived
        if auto_approve is not None:
            if thread.workflow_step_id is None:
                raise ValueError(
                    "auto-approval is only available for workflow conversations"
                )
            thread.auto_approve = auto_approve
        await self._persist_metadata(thread)
        if auto_approve:
            await self.approvals.auto_approve_pending(instance_id)
        return thread

    async def _persist_metadata(self, thread: ChatThread) -> None:
        await self.session.update_instance_metadata(
            thread.instance_id,
            thread.title,
            thread.archived,
            thread.runner,
            thread.auto_approve,
        )

    async def _require(self, instance_id: AgentInstanceId) -> ChatThread:
        thread = await self.get(instance_id)
        if thread is None:
            raise KeyError(f"no chat thread {instance_id!r}")
        return thread

    async def require_somewhere_to_run(self, instance_id: AgentInstanceId) -> None:
        """Refuse a turn a detached chat cannot run, in words the UI can act on.

        The runner would fail on the missing directory anyway, several layers
        down and phrased as a lookup error. A chat that never had a workspace
        is left alone: it runs where the process was told to.
        """
        try:
            workspace = await self.session.workspace(instance_id)
            detached = workspace is not None and not workspace.attached
        except KeyError:
            detached = True
        if detached:
            raise RuntimeError(
                "this chat's worktree is detached; reattach it to run the agent"
            )

    async def _sync_workspace(self, thread: ChatThread) -> ChatThread:
        """Record what the provider currently says about this chat's workspace.

        Conversations outlive their checkouts -- `git worktree remove`, a swept
        /tmp, a reboot -- so a chat is listed with whatever is left of its
        workspace rather than failing the request. A provider that disowns the
        id entirely is treated the same way: the chat is simply one without a
        workspace, and attaching offers it a new one.
        """
        try:
            return _with_workspace(
                thread, await self.session.workspace(thread.instance_id)
            )
        except KeyError:
            return _with_workspace(thread, None)

    async def _restore(self) -> None:
        """Populate the UI registry from the durable conversation store once."""
        if self._restored:
            return
        async with self._restore_lock:
            if self._restored:
                return
            instances = await self.session.instances()
            for instance in reversed(instances):
                thread = ChatThread(
                    instance.instance_id,
                    instance.agent_id,
                    (
                        instance.runner
                        if instance.runner in self.session.runners
                        else self.session.default_runner
                    ),
                    title=await self._instance_title(instance),
                    archived=instance.archived,
                    workflow_run_id=instance.workflow_run_id,
                    workflow_step_id=instance.workflow_step_id,
                    editable=await self._instance_step_editable(instance),
                    auto_approve=instance.auto_approve,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            # A CLI subprocess does not survive the server that spawned it, so
            # a request still marked pending here was asked by a process that
            # no longer exists and can never be answered.
            await self.approvals.interrupt_orphans()
            self._restored = True

    async def _instance_step_editable(self, instance: AgentInstance) -> bool:
        if instance.workflow_run_id is None or instance.workflow_step_id is None:
            return False
        state = await self.session.state_store.load(instance.workflow_run_id)
        if state is None:
            return False
        definition = state.workflow_definition
        if definition is None and self._workflow_catalog is not None:
            definition = self._workflow_catalog.get(state.workflow_id)
        return _workflow_step_editable(definition, instance.workflow_step_id)

    async def _instance_title(self, instance: AgentInstance) -> str:
        """Name workflow conversations after their owning run."""
        if instance.workflow_run_id is None or instance.title != "New chat":
            return instance.title
        state = await self.session.state_store.load(instance.workflow_run_id)
        if state is None:
            return instance.title
        return state.name or state.prompt or str(state.run_id)


#: What the dropdown puts in front of a graph workflow's name.
#:
#: Plain English: these workflows are new and not finished yet. The label is
#: there so nobody picks one expecting it to behave like the ones that have
#: been running for months. It is a prefix on the *name* rather than a separate
#: field so that every list, however it is drawn, carries the warning.
BETA = "[BETA]"

#: Where the graph runtime's sub-application is served from, so its addresses
#: are `/graph/api/runs/...` and cannot collide with this app's own `/api`.
GRAPH_PREFIX = "/graph"

#: The two things the graph engine says that change what a WorkOrder row should
#: read: it finished, or it stopped. Everything else it says is about positions
#: inside the graph, which this app's row has no way to show.
GRAPH_ENDINGS: Mapping[EventKind, RunPhase] = {
    EventKind.RUN_FINISHED: RunPhase.SUCCEEDED,
    EventKind.RUN_FAILED: RunPhase.FAILED,
}

#: The same three answers, as the graph engine reports them when asked rather
#: than when it announces them. A run waiting on a person is still working as
#: far as a WorkOrder row is concerned: what it is waiting for is a question
#: only the graph engine's own API can show today.
GRAPH_PHASES: Mapping[RunStatus, RunPhase] = {
    RunStatus.RUNNING: RunPhase.RUNNING_AGENT,
    RunStatus.AWAITING_APPROVAL: RunPhase.RUNNING_AGENT,
    RunStatus.COMPLETED: RunPhase.SUCCEEDED,
    RunStatus.FAILED: RunPhase.FAILED,
}


#: Where this module says what went wrong with something nobody asked it about
#: -- a graph engine that would not open, a stranded run it could not pick back
#: up. Those go to the log rather than to a person, because the person who
#: would read them is not in the room when a server starts.
log = logging.getLogger(__name__)


@dataclass(slots=True)
class _GraphSurface:
    """The graph engine, once the server has started it.

    Both fields are empty until the application starts, because opening the
    engine means opening files and that is something a running server owns
    rather than something building one does. A request that arrives before then
    is told the graph engine is not running rather than being given half of it.

    They stay empty when the engine could not be opened for a reason outside
    the graphs themselves, which is what keeps that kind of failure to the
    `[BETA]` feature: no engine, no `[BETA]` entries in the dropdown, and the
    rest of the application carries on. A graph that does not *compile* never
    gets this far -- it stops the server, because it is a definition somebody
    has to fix.
    """

    runtime: GraphRuntime | None = None
    app: Starlette | None = None


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
    *,
    workflow_runners: Mapping[str, AgentRunner] | None = None,
    review_runners: Mapping[str, AgentRunner] | None = None,
    workflow_catalog: WorkflowCatalog | None = None,
    graph_runtime: AbstractAsyncContextManager[GraphRuntime] | None = None,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    default_branch: str = "main",
    credential_store: GitHubCredentialStore | None = None,
    github_client_id: str = "",
    github_client_id_source: str = "configuration",
    source_control_preferences: SourceControlPreferences | None = None,
    slack_credential_store: SlackCredentialStore | None = None,
    communications_channel: str = "",
    public_url: str = "",
    work_orders: WorkOrdersConfig = WorkOrdersConfig(),
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    if workflow_runners is not None and review_runners is None:
        raise ValueError("review_runners are required with workflow_runners")
    if workflow_catalog is None:
        loaded_config = load_engine_config()
        catalog = (
            load_workflow_catalog(loaded_config.workflows_directory)
            if loaded_config.workflows_directory is not None
            else WorkflowCatalog.from_definitions(())
        )
    else:
        catalog = workflow_catalog
    # The graph workflows this deployment could run, looked up by the id the
    # dropdown sends back. A graph is the newer kind of workflow: the catalog
    # keeps it apart from the step workflows because a different engine runs
    # it, and this is the interface's half of that -- the one list a person
    # picks from, with each entry remembering which engine it belongs to.
    #
    # "Could", not "does": whether they are actually offered is `offered_graphs`
    # below, which additionally asks whether the engine is running.
    graph_workflows: Mapping[str, GraphWorkflow] = (
        {str(graph.graph_id): graph for graph in catalog.graphs}
        if graph_runtime is not None
        else {}
    )
    surface = _GraphSurface()
    # Filled by the graph engine while a run is going, and read by the feed the
    # graph's own sub-application serves. Built here rather than when the
    # server starts so that the observer below can be written once.
    graph_events = EventLog()

    def offered_graphs() -> Mapping[str, GraphWorkflow]:
        """The `[BETA]` entries a person may pick, right now.

        Two things have to be true, and the second one is only knowable once
        the server is up: this deployment has graph workflows, and the engine
        that runs them opened. If it did not -- an unwritable state directory,
        a graph that no longer compiles -- there are no `[BETA]` entries at
        all, rather than entries that fail the moment somebody picks one.
        """
        return graph_workflows if surface.runtime is not None else {}

    approval_feed = ApprovalFeed(session.state_store)
    service = ThreadService(
        session,
        runners,
        catalog,
        approval_policy,
        approval_observer=approval_feed.publish,
    )
    run_reader = RunReader(session.state_store, catalog)

    async def approval_presented(_approval: ApprovalRecord) -> None:
        # The broker's observer publishes every persisted transition. This
        # presenter exists because the runner callback also supports run-local
        # presentation, which workflow runs do not need.
        return None

    def workflow_approval_handler(
        command: StartAgentRun, runner_name: str
    ) -> ApprovalHandler:
        # A workflow step runs on the implementation or the review runner of one
        # provider, and the two read that provider's requests the same way --
        # so either mapping answers "what does `runner_name` speak".
        step_runner = (workflow_runners or runners).get(runner_name) or runners.get(
            runner_name
        )
        return service.approvals.handler(
            agent_run_id=command.agent_run_id,
            instance_id=command.instance_id,
            runner=runner_name,
            present=approval_presented,
            workspace_id=command.workspace_id,
            translator=(
                step_runner.permission_translator if step_runner is not None else None
            ),
            auto_approve=lambda: service.auto_approve_enabled(command.instance_id),
            # The command carries the profile it was started with, so a step
            # running an agent that only reads is held to that here as well as
            # in a chat -- including against the conversation's own auto-approve.
            read_only=command.profile.read_only,
        )

    workflow_executor = WorkflowExecutor(
        session.capabilities,
        workflow_runners if workflow_runners is not None else runners,
        review_runners=review_runners if review_runners is not None else runners,
        approval_handler=workflow_approval_handler,
        catalog=catalog,
        default_branch=default_branch,
        communications_channel=communications_channel,
        public_url=public_url,
    )
    workflow_tasks: dict[RunId, asyncio.Task[None]] = {}
    workflow_restart_locks: dict[RunId, asyncio.Lock] = {}

    def track_workflow(run_id: RunId, task: asyncio.Task[None]) -> None:
        workflow_tasks[run_id] = task
        task.add_done_callback(
            lambda completed: (
                workflow_tasks.pop(run_id, None)
                if workflow_tasks.get(run_id) is completed
                else None
            )
        )

    async def workflow_runner_for(state: RunState) -> str:
        """Use the active conversation's runner before the run's initial choice."""

        instances = await session.state_store.list_instances(
            workflow_run_id=state.run_id
        )
        current = next(
            (
                instance
                for instance in instances
                if instance.workflow_step_id == state.current_step_id
            ),
            None,
        )
        previous = next((instance for instance in instances if instance.runner), None)
        return (
            current.runner
            if current is not None and current.runner
            else state.runner_name
            or (previous.runner if previous is not None else "")
            or workflow_executor.default_runner
        )

    async def restore_agent_steps() -> None:
        """Restart agent commands whose process-local dispatch was lost."""
        for state in await session.state_store.list_runs():
            if (
                state.phase is not RunPhase.RUNNING_AGENT
                or state.agent_paused
                or state.run_id in workflow_tasks
                # A graph WorkOrder is not this executor's to restart: it has
                # no steps to pick back up, and looking for a step list a graph
                # does not have would fail a run that is perfectly healthy.
                # `restore_graph_runs` is the one that picks these up.
                or str(state.workflow_id) in graph_workflows
            ):
                continue
            runner_name = await workflow_runner_for(state)
            track_workflow(
                state.run_id,
                asyncio.create_task(
                    workflow_executor.resume_agent_step(
                        state.run_id, runner_name=runner_name
                    )
                ),
            )

    async def graph_event(event: RuntimeEvent) -> None:
        """Everything the graph engine says, kept where two readers can see it.

        The feed is one reader: a browser or a script watching a graph run gets
        these back in order from the sub-application below.

        The WorkOrder row is the other. A graph run keeps its real progress in
        the graph engine's own files, and this app only holds a row for it, so
        without this the row would say "an agent is working" long after the run
        had finished or fallen over. Only the two endings are copied across;
        the rest of what a graph says is about positions inside the graph, and
        a row has nowhere to put them.
        """
        await graph_events.append(event)
        phase = GRAPH_ENDINGS.get(event.kind)
        if phase is None:
            return
        state = await session.state_store.load(event.run_id)
        if state is None:
            return
        await session.state_store.save(
            replace(
                state,
                phase=phase,
                failure_reason=str(event.payload.get("error", ""))
                or state.failure_reason,
            )
        )

    async def restore_graph_runs(runtime: GraphRuntime) -> None:
        """Pick every unfinished graph WorkOrder back up, or say why it cannot be.

        A run's progress lives in the graph engine's files, but the *driver* --
        the thing actually working through the graph -- is a task in a process,
        and a process that stops takes its drivers with it. Nothing rebuilds
        them on its own, so without this a run that was mid-agent when the
        server was restarted would sit at "working" forever, saying nothing and
        doing nothing.

        Three answers, one per thing the engine can say about a run:

        * **working** -- there is no driver for it in this fresh process, so it
          is sent back to the last position it saved and carried on from there.
          Whatever the interrupted agent had done since that position is lost,
          which is the honest cost of the process having died mid-sentence;
        * **waiting on a person** -- left exactly as it is. Answering the
          question is what starts it again, and that already works;
        * **finished or failed** -- the row missed the ending because the
          process was gone when it was announced, so it is copied over now.

        A run the engine has never heard of is one whose state was deleted from
        under it. It cannot be recovered and cannot be waited for, so the row is
        failed with a reason rather than left claiming to be working.
        """
        for state in await session.state_store.list_runs():
            if state.is_terminal or str(state.workflow_id) not in graph_workflows:
                continue
            try:
                snapshot = await runtime.snapshot(state.run_id)
                if snapshot is None:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=RunPhase.FAILED,
                            failure_reason=(
                                "the graph engine has no record of this run"
                            ),
                        )
                    )
                    continue
                if snapshot.status is RunStatus.RUNNING:
                    # A restart is the only way to be here: a run that is
                    # working has a driver, and this runs before any request
                    # could have started one.
                    if snapshot.checkpoint_id is None:
                        continue
                    await runtime.resume_from(state.run_id, snapshot.checkpoint_id)
                    continue
                phase = GRAPH_PHASES[snapshot.status]
                if phase is not state.phase or snapshot.error != state.failure_reason:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=phase,
                            failure_reason=snapshot.error or state.failure_reason,
                        )
                    )
            except Exception:
                # One unrecoverable run must not stop the others from being
                # recovered, and none of them may stop the server from serving.
                log.exception("could not restore graph WorkOrder %s", state.run_id)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as opened:
            if graph_runtime is not None:
                # Opening the graph engine is what makes a `[BETA]` WorkOrder
                # startable: it compiles every graph in the workflow directory
                # and opens the files they remember their progress in. The exit
                # stack closes it again when the server stops, which is the
                # only thing that closes those files.
                #
                # It can fail two ways, and they are not the same kind of news.
                #
                # A graph that does not compile is a broken definition: a file
                # in this deployment's workflow directory says something that is
                # not a graph. Nothing about it improves by carrying on, and a
                # server that quietly dropped it would be running a deployment
                # nobody configured. So the graph is named, the reason is logged
                # in full, and startup fails -- loudly, at the moment somebody
                # is looking, rather than the first time a person picks it.
                #
                # Anything else is the environment around the graphs rather than
                # the graphs themselves: a state directory this process cannot
                # write, a checkpoint file another process is holding. That is
                # not a reason for chats, projects and the step WorkOrders to go
                # down with it, so it is logged and contained -- no engine, and
                # therefore no `[BETA]` entries offered anywhere.
                try:
                    surface.runtime = await opened.enter_async_context(graph_runtime)
                except GraphCompilationError as broken:
                    log.error(
                        "%s workflow %r does not compile, so this server will "
                        "not start: %s",
                        BETA,
                        str(broken.graph_id),
                        broken.reason,
                        exc_info=True,
                    )
                    raise
                except Exception:
                    log.exception(
                        "the graph engine did not start; %s WorkOrders are not "
                        "being offered in this process",
                        BETA,
                    )
                else:
                    # The graph engine's own control surface, so a run started
                    # here can be watched and answered. Built first because it
                    # installs a listener of its own, and this app wants that
                    # listener *and* the WorkOrder row kept up to date -- so
                    # ours is installed afterwards and does both.
                    surface.app = create_graph_app(surface.runtime, graph_events)
                    surface.runtime.observe(graph_event)
                    await restore_graph_runs(surface.runtime)
            await restore_agent_steps()
            try:
                yield
            finally:
                tasks = tuple(workflow_tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def workflow_is_active(thread: ChatThread) -> bool:
        return (
            thread.workflow_run_id is not None
            and thread.workflow_run_id in workflow_tasks
        )

    async def interrupt_workflow(thread: ChatThread) -> None:
        """Stop the active process for an editable step without failing its run."""

        if not thread.editable or thread.workflow_run_id is None:
            raise RuntimeError("this workflow conversation is read-only")
        state = await session.state_store.load(thread.workflow_run_id)
        if (
            state is None
            or state.phase is not RunPhase.RUNNING_AGENT
            or state.current_step_id != thread.workflow_step_id
        ):
            raise RuntimeError("this workflow step is no longer active")
        if state.current_agent_run_id is not None:
            await service.approvals.cancel_run(state.current_agent_run_id)
        task = workflow_tasks.get(thread.workflow_run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert thread.workflow_step_id is not None
        await workflow_executor.pause_agent_step(
            thread.workflow_run_id, thread.workflow_step_id
        )

    async def switch_workflow_runner(thread: ChatThread) -> None:
        """Restart an active workflow turn on its conversation's new runner."""

        assert thread.workflow_run_id is not None
        lock = workflow_restart_locks.setdefault(thread.workflow_run_id, asyncio.Lock())
        async with lock:
            task = workflow_tasks.get(thread.workflow_run_id)
            if task is None or task.done():
                return
            state = await session.state_store.load(thread.workflow_run_id)
            if (
                state is None
                or state.phase is not RunPhase.RUNNING_AGENT
                or state.current_step_id != thread.workflow_step_id
            ):
                return
            if state.current_agent_run_id is not None:
                await service.approvals.cancel_run(state.current_agent_run_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            # The completed turn may have advanced to another agent between the
            # first state read and cancellation. Resume whichever conversation is
            # now current, without applying this conversation's choice to another.
            state = await session.state_store.load(thread.workflow_run_id)
            if (
                state is None
                or state.phase is not RunPhase.RUNNING_AGENT
                or state.agent_paused
            ):
                return
            runner_name = await workflow_runner_for(state)
            track_workflow(
                state.run_id,
                asyncio.create_task(
                    workflow_executor.resume_agent_step(
                        state.run_id, runner_name=runner_name
                    )
                ),
            )

    async def continue_workflow(thread: ChatThread, text: str) -> None:
        """Interrupt, append a human message, and resume the same workflow step."""

        assert thread.workflow_run_id is not None
        if not thread.editable:
            raise RuntimeError("this workflow conversation is read-only")
        await service.require_somewhere_to_run(thread.instance_id)
        before = len(await service.history(thread.instance_id))
        state = await session.state_store.load(thread.workflow_run_id)
        if state is None:
            raise RuntimeError("this workflow step is no longer active")
        if state.current_agent_run_id is not None:
            await service.approvals.cancel_run(state.current_agent_run_id)
        task = workflow_tasks.get(thread.workflow_run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if (
            state.phase is RunPhase.RUNNING_AGENT
            and state.current_step_id == thread.workflow_step_id
        ):
            await workflow_executor.pause_agent_step(
                thread.workflow_run_id, thread.workflow_step_id
            )
        task = asyncio.create_task(
            workflow_executor.resume_agent_step(
                thread.workflow_run_id,
                text,
                thread.runner,
                step_id=thread.workflow_step_id,
            )
        )
        track_workflow(thread.workflow_run_id, task)
        while (
            len(await service.history(thread.instance_id)) <= before and not task.done()
        ):
            await asyncio.sleep(0)
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise RuntimeError(str(task.exception()))

    async def stream_workflow_conversation(
        instance_id: AgentInstanceId, run_id: RunId
    ) -> AsyncIterator[bytes]:
        """Poll durable workflow progress into the chat client's snapshot stream."""
        previous: list[dict[str, object]] | None = None
        previous_approvals: dict[str, dict[str, object]] = {}
        while True:
            history = await service.history(instance_id)
            content = _latest_assistant_content(history)
            approvals = await session.state_store.list_approvals(
                instance_id=instance_id
            )
            active = run_id in workflow_tasks
            for record in approvals:
                approval = _approval_json(record)
                approval_id = str(record.approval_id)
                if approval != previous_approvals.get(approval_id):
                    previous_approvals[approval_id] = approval
                    yield _json_line({"type": "approval", "approval": approval})
            if not active:
                yield _json_line({"type": "done", "content": content})
                return
            if content != previous:
                previous = content
                yield _json_line({"type": "content", "content": content})
            await asyncio.sleep(0.25)

    async def config(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "agents": [
                    {
                        "id": str(agent_id),
                        "description": profile.description,
                        "instructions": profile.instructions,
                    }
                    for agent_id, profile in sorted(session.profiles.items())
                ],
                "runners": [
                    {"id": name, "implementation": type(runner).__name__}
                    for name, runner in runners.items()
                ],
                "defaultAgent": str(next(iter(sorted(session.profiles)))),
                # Which agent the New Project button starts a conversation with, named
                # here rather than in the client so the id stays one thing this
                # process owns. Empty when no such profile is composed, which is
                # the client's cue that there is nothing to plan with.
                "planAgent": (
                    str(PLANNER.agent_id)
                    if PLANNER.agent_id in session.profiles
                    else ""
                ),
                "defaultRunner": session.default_runner,
                "workflowRunners": list(workflow_executor.runners),
                "defaultWorkflowRunner": workflow_executor.default_runner,
                # One dropdown, two kinds of workflow. The step workflows come
                # first and read as they always have; the graph ones follow,
                # wearing `[BETA]` and no version, because a graph does not
                # have one yet. Only the graphs this process can actually start
                # are here -- see `offered_graphs` -- because an entry nobody
                # could run would be a choice that fails after it was made.
                #
                # `kind` is what each entry belongs to, said rather than left to
                # be guessed. The form reads it: a graph names its own agent, so
                # the runner field is not shown for one, and a client that
                # worked that out from the empty version would break the day a
                # graph gets versioned.
                "workflows": [
                    {
                        "id": str(definition.workflow_id),
                        "name": definition.name,
                        "version": definition.version,
                        "kind": "steps",
                    }
                    for definition in catalog
                ]
                + [
                    {
                        "id": str(graph.graph_id),
                        "name": f"{BETA} {graph.name}",
                        "version": "",
                        "kind": "graph",
                    }
                    for graph in offered_graphs().values()
                ],
            }
        )

    async def list_threads(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"threads": [_thread_json(t) for t in await service.list()]}
        )

    async def list_runs(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"runs": [_run_json(run, listing=True) for run in await run_reader.list()]}
        )

    async def open_conversations() -> set[AgentInstanceId]:
        """The plans a project row can be linked to.

        A project is reached through the planning conversation it was named
        after. Resolved against the threads that can be opened rather than
        spelled from the id alone: a project recorded some other way has the
        same shape and no conversation, and an archived plan's page is a blank
        new chat rather than the plan.
        """
        return {
            thread.instance_id for thread in await service.list() if not thread.archived
        }

    async def list_projects(_request: Request) -> JSONResponse:
        projects = await session.state_store.list_projects()
        conversations = await open_conversations()
        # A project's milestones are offered in the rail only by the projects
        # that have some, so the list says how many. Counted by the store, in
        # one grouped query: the shell polls this route every second, and both
        # a query per row and a read of every milestone would make that cost
        # grow -- with the list, or with the size of every plan in it.
        milestones = await session.state_store.count_milestones_by_project()
        return JSONResponse(
            {
                "projects": [
                    _project_json(
                        project,
                        conversations,
                        milestones=milestones.get(project.project_id, 0),
                    )
                    for project in projects
                ]
            }
        )

    async def create_project(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            name = _required_string(body, "name")
        except ValueError as error:
            return _error(str(error), 400)
        project = Project(ProjectId(f"project-{uuid4().hex[:12]}"), name[:80])
        await session.state_store.save_project(project)
        # Recorded rather than planned, so it owns no conversation to link to
        # and nothing has been planned under it yet.
        return JSONResponse(_project_json(project, (), milestones=0), status_code=201)

    async def archive_project(request: Request) -> JSONResponse:
        """Put a project away, or take it back out, from one pair of routes.

        Which one was asked for is read from the path, the way archiving a chat
        already is: the two differ only in the flag they record.
        """
        project_id = ProjectId(request.path_params["project_id"])
        project = await session.state_store.load_project(project_id)
        if project is None:
            return _error("project not found", 404)
        archived = request.url.path.rsplit("/", 1)[-1] == "archive"
        project = replace(project, archived=archived)
        await session.state_store.save_project(project)
        # An archived project keeps its plan: restoring puts the link and the
        # milestones back rather than leaving a row that has forgotten where it
        # went. The answer is the whole row the list would send -- counted the
        # same way, so the two cannot drift -- and a client that redraws from it
        # is not left with a project missing half itself.
        counts = await session.state_store.count_milestones_by_project()
        return JSONResponse(
            _project_json(
                project,
                await open_conversations(),
                milestones=counts.get(project_id, 0),
            )
        )

    async def list_project_milestones(request: Request) -> JSONResponse:
        project_id = ProjectId(request.path_params["project_id"])
        project = await session.state_store.load_project(project_id)
        if project is None:
            return _error("project not found", 404)
        milestones = await session.state_store.list_milestones(project_id)
        # Read every workstream once and group here rather than asking per
        # milestone: the timeline polls this route every second per open
        # project, and a query per milestone makes that cost grow with the plan
        # while holding the store's lock.
        by_milestone = workstreams_by_milestone(
            await session.state_store.list_workstreams()
        )
        return JSONResponse(
            {
                # Linked to its plan like any other row: the milestones page
                # this answers is where the way back to the conversation is.
                "project": _project_json(project, await open_conversations()),
                "milestones": [
                    _milestone_json(
                        milestone, by_milestone.get(milestone.milestone_id, ())
                    )
                    for milestone in milestones
                ],
            }
        )

    async def start_graph_run(
        runtime: GraphRuntime,
        graph: GraphWorkflow,
        *,
        prompt: str,
        repository: str,
        workstream_id: WorkstreamId | None,
        milestone_id: MilestoneId | None,
    ) -> JSONResponse:
        """Hand a `[BETA]` WorkOrder to the graph engine and keep a row for it.

        What actually starts the work is one call: the graph engine is given
        the graph's id and the two things every one of these graphs asks for --
        the task to do, and the repository to do it in. It provisions the
        checkout, runs the agents and stops for a person by itself, and it
        remembers all of that in its own files.

        The row saved afterwards is this app's, and it is a record rather than
        a driver: it is what puts the WorkOrder in the list, on the sidebar and
        at a URL. It carries the graph engine's own run id, so the two halves
        are talking about the same run and nothing has to translate between two
        sets of ids.

        No runner is passed on, because a graph already names the agent it runs
        -- picking "Implementation review (claude)" *is* picking Claude, which
        is why there is one entry per agent in the dropdown rather than a
        separate choice, and why the form hides the runner field for one.

        The engine is an argument rather than something read here, because
        having one is what made this graph offerable in the first place: a
        caller that got a graph out of `offered_graphs` has already established
        that the engine is running, and passing it on says so.
        """
        snapshot = await runtime.start(
            GraphId(str(graph.graph_id)),
            {"task": prompt, "repository": repository},
        )
        state = RunState(
            run_id=snapshot.run_id,
            task_id=TaskId(f"task-{uuid4().hex[:12]}"),
            workflow_id=WorkflowId(str(graph.graph_id)),
            workstream_id=workstream_id,
            milestone_id=milestone_id,
            # Working, as the engine has just reported it. `graph_event` above
            # moves this when the run ends. It is never picked back up by the
            # step executor -- see `restore_agent_steps`.
            phase=GRAPH_PHASES[snapshot.status],
            prompt=prompt,
            repository=repository,
        )
        await session.state_store.save(state)
        # A very short run can be over before the row above exists, and the
        # ending it announced would then have had nothing to land on -- leaving
        # a WorkOrder that claims to be working forever. So the engine is asked
        # once more, now that there is a row for its answer.
        latest = await runtime.snapshot(state.run_id)
        if latest is not None and GRAPH_PHASES[latest.status] is not state.phase:
            state = replace(
                state,
                phase=GRAPH_PHASES[latest.status],
                failure_reason=latest.error,
            )
            await session.state_store.save(state)
        run = await run_reader.get(state.run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def create_run(request: Request) -> JSONResponse:
        """Persist a workflow request and start its supported local execution."""
        body = await _json_body(request)
        try:
            prompt = _required_string(body, "prompt")
            repository = _required_string(body, "repository")
            workflow_id = WorkflowId(_required_string(body, "workflowId"))
            workstream_value = _optional_string(body, "workstreamId")
            milestone_value = _optional_string(body, "milestoneId")
        except ValueError as error:
            return _error(str(error), 400)
        definition = catalog.get(workflow_id)
        graph = offered_graphs().get(str(workflow_id))
        if definition is None and graph is None:
            return _error(f"unknown workflow definition: {workflow_id}", 400)
        runner_name = str(body.get("runner") or workflow_executor.default_runner)
        # A graph names the agent it runs, so there is no runner to check and
        # none is sent: the form does not offer the field for one. Validating
        # it anyway would refuse a WorkOrder over a value nothing reads.
        if graph is None and runner_name not in workflow_executor.runners:
            return _error(f"unknown workflow runner: {runner_name}", 400)
        workstream_id = (
            WorkstreamId(workstream_value) if workstream_value is not None else None
        )
        milestone_id = (
            MilestoneId(milestone_value) if milestone_value is not None else None
        )
        workstream = (
            await session.state_store.load_workstream(workstream_id)
            if workstream_id is not None
            else None
        )
        if workstream_id is not None and workstream is None:
            return _error(f"unknown workstream: {workstream_id}", 400)
        if milestone_id is not None:
            milestone = await session.state_store.load_milestone(milestone_id)
            if milestone is None:
                return _error(f"unknown milestone: {milestone_id}", 400)
            if workstream is not None and workstream.milestone_id != milestone_id:
                return _error(
                    f"workstream {workstream_id} does not belong to milestone {milestone_id}",
                    400,
                )

        # A selected workstream is the more specific relationship. The
        # milestone is retained only for a task created without one.
        direct_milestone_id = milestone_id if workstream_id is None else None

        if graph is not None:
            # `offered_graphs` only answers with a graph while the engine is
            # running, so this cannot be `None` here.
            assert surface.runtime is not None
            return await start_graph_run(
                surface.runtime,
                graph,
                prompt=prompt,
                repository=repository,
                workstream_id=workstream_id,
                milestone_id=direct_milestone_id,
            )

        state = await start_step_run(
            prompt=prompt,
            repository=repository,
            workflow_id=workflow_id,
            definition=definition,
            runner_name=runner_name,
            workstream_id=workstream_id,
            milestone_id=direct_milestone_id,
        )
        run = await run_reader.get(state.run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def start_step_run(
        *,
        prompt: str,
        repository: str,
        workflow_id: WorkflowId,
        definition: WorkflowDefinition | None,
        runner_name: str,
        workstream_id: WorkstreamId | None = None,
        milestone_id: MilestoneId | None = None,
        origin: RunOrigin | None = None,
    ) -> RunState:
        """Record a step WorkOrder and start driving it, whoever asked for it.

        The form and a chat mention differ in what they know, not in what they
        start -- so this is one function rather than two that would drift: an
        `origin` is the only thing the second one carries that the first does
        not, and it is what makes the run answerable in the place it came from.
        """
        run_id = RunId(f"run-{uuid4().hex[:12]}")
        task_id = TaskId(f"task-{uuid4().hex[:12]}")
        event = RunRequested(
            run_id=run_id,
            task_id=task_id,
            prompt=prompt,
            repository=repository,
            workflow_id=workflow_id,
            workstream_id=workstream_id,
            milestone_id=milestone_id,
        )
        state = RunState(
            run_id=run_id,
            task_id=task_id,
            workflow_id=workflow_id,
            workstream_id=workstream_id,
            milestone_id=milestone_id,
            prompt=prompt,
            repository=repository,
            workflow_definition=definition,
            origin=origin,
        )
        await session.state_store.save(state)
        await session.state_store.append_events(run_id, (event,))
        track_workflow(
            run_id,
            asyncio.create_task(workflow_executor.start(event, runner_name)),
        )
        return state

    async def get_run(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        run = await run_reader.get(run_id)
        if run is None:
            return _error("run not found", 404)
        task = workflow_tasks.get(run_id)
        if run.terminal_outcome is not None and task is not None and not task.done():
            # A terminal MCP result is persisted before its acknowledgement is
            # sent. Do not expose the terminal snapshot until that final piece
            # of the runner protocol has completed too.
            await asyncio.shield(task)
            run = await run_reader.get(run_id)
            assert run is not None
        return JSONResponse(_run_json(run))

    async def delete_run(request: Request) -> Response:
        """Throw a WorkOrder away, whatever it was in the middle of.

        A run still being worked on is stopped first, and which engine is asked
        to stop it depends on which one is running it. A step WorkOrder is this
        app's: the agent turn is cancelled and the task driving the run is
        awaited out, so nothing is left holding a run id that is about to stop
        existing -- a save landing after the delete would put the row back, and
        the WorkOrder the reader just threw away would reappear on the next
        poll.

        A `[BETA]` one is the graph engine's, and none of that reaches it: its
        driver is a task in the engine, not in `workflow_tasks`, and the agent
        it has open is not an agent run this app started. Deleting the row
        without telling the engine would take the WorkOrder off the rail and
        leave the run working -- agents still going in the repository, with
        nothing left on screen to stop them by. So the engine is asked to
        cancel the run, and only then is the row forgotten.
        """
        run_id = RunId(request.path_params["run_id"])
        state = await session.state_store.load(run_id)
        if state is None:
            return _error("run not found", 404)
        if str(state.workflow_id) in graph_workflows:
            await cancel_graph_run(run_id)
        if state.current_agent_run_id is not None:
            await service.approvals.cancel_run(state.current_agent_run_id)
        task = workflow_tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await session.state_store.delete_run(run_id)
        return Response(status_code=204)

    async def cancel_graph_run(run_id: RunId) -> None:
        """Stop a `[BETA]` WorkOrder in the engine, if there is one to stop.

        Two ways there is nothing to do, and neither is a reason to refuse the
        delete. The engine may not be running at all -- it failed to open, or
        this process never had one -- in which case nothing here is driving the
        run either, because a driver is a task in a process. And the engine may
        not know the run: a row whose graph state was deleted from under it,
        which `restore_graph_runs` fails on startup for the same reason.

        Either way the row is the reader's to throw away, so the reason is
        logged and the delete goes on. Refusing would leave a WorkOrder nobody
        can remove and nothing is working on.
        """
        runtime = surface.runtime
        if runtime is None:
            log.warning(
                "the graph engine is not running, so %s WorkOrder %s was "
                "deleted without being cancelled",
                BETA,
                run_id,
            )
            return
        try:
            await runtime.cancel(run_id)
        except GraphRuntimeError:
            log.warning(
                "the graph engine has no record of %s WorkOrder %s, so there "
                "was nothing to cancel",
                BETA,
                run_id,
            )

    async def graph_run_events(request: Request) -> JSONResponse:
        """Replay the graph transcript for the WorkOrder UI.

        The graph control surface deliberately exposes a live event stream. The
        WorkOrder page also needs a finite snapshot when it opens after an
        agent has finished, so serve the same recorded events as JSON here.
        """
        run_id = RunId(request.path_params["run_id"])
        state = await session.state_store.load(run_id)
        if state is None:
            return _error("run not found", 404)
        if str(state.workflow_id) not in graph_workflows:
            return _error("run is not a graph WorkOrder", 409)
        return JSONResponse(
            {
                "events": [
                    {
                        "sequence": event.sequence,
                        "type": event.kind.value,
                        "nodeId": str(event.node_id) if event.node_id else None,
                        "payload": dict(event.payload),
                    }
                    for event in graph_events.since(run_id)
                ]
            }
        )

    async def complete_human_review(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        state = await session.state_store.load(run_id)
        if state is None:
            return _error("run not found", 404)
        body = await _json_body(request)
        approved = body.get("approved")
        if not isinstance(approved, bool):
            return _error("approved must be a boolean", 400)
        if (
            state.phase is not RunPhase.AWAITING_HUMAN_REVIEW
            or state.current_step_id is None
        ):
            return _error("run is not awaiting human review", 409)
        summary = str(body.get("summary", "")).strip()
        try:
            next_state = await workflow_executor.complete_human_review(
                HumanReviewCompleted(
                    run_id=run_id,
                    step_id=state.current_step_id,
                    approved=approved,
                    summary=summary,
                )
            )
            if next_state.phase is RunPhase.RUNNING_AGENT:
                track_workflow(
                    run_id,
                    asyncio.create_task(workflow_executor.resume_agent_step(run_id)),
                )
        except WorkflowExecutionError as error:
            return _error(str(error), 409)
        run = await run_reader.get(run_id)
        assert run is not None
        return JSONResponse(_run_json(run))

    async def create_thread(request: Request) -> JSONResponse:
        body = await _json_body(request)
        create_project = body.get("createProject", False)
        if not isinstance(create_project, bool):
            return _error("createProject must be a boolean", 400)
        try:
            thread = await service.create(
                AgentId(_required_string(body, "agentId")),
                _required_string(body, "runner"),
            )
        except (KeyError, ValueError) as error:
            return _error(str(error), 400)
        if create_project:
            # Finish the durable intent before returning the thread id. The
            # client cannot replace /plan with its permalink until this request
            # resolves, so closing or reloading during the slower title request
            # cannot strand an ordinary chat without its project.
            thread = await service.update_metadata(
                thread.instance_id, title="New project"
            )
            await session.state_store.save_project(
                Project(project_id_for_instance(thread.instance_id), thread.title)
            )
        return JSONResponse(_thread_json(thread), status_code=201)

    async def get_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        return JSONResponse(_thread_json(thread))

    async def update_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        title = None
        if "title" in body:
            title = str(body["title"]).strip()
            if title:
                title = title[:80]
            else:
                title = None
        runner = str(body["runner"]) if "runner" in body else None
        if (
            runner is not None
            and thread.workflow_run_id is not None
            and runner not in workflow_executor.runners
        ):
            return _error(f"unknown workflow runner: {runner}", 400)
        runner_changed = runner is not None and runner != thread.runner
        auto_approve = None
        if "autoApprove" in body:
            if not isinstance(body["autoApprove"], bool):
                return _error("autoApprove must be a boolean", 400)
            auto_approve = body["autoApprove"]
        try:
            thread = await service.update_metadata(
                instance_id,
                title=title,
                runner=runner,
                auto_approve=auto_approve,
            )
        except ValueError as error:
            return _error(str(error), 400)
        if runner_changed and thread.workflow_run_id is not None:
            await switch_workflow_runner(thread)
        return JSONResponse(_thread_json(thread))

    async def archive_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        thread = await service.update_metadata(
            thread.instance_id,
            archived=request.url.path.rsplit("/", 1)[-1] == "archive",
        )
        return JSONResponse(_thread_json(thread))

    async def delete_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        await service.delete(instance_id)
        return Response(status_code=204)

    async def messages(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        history = await service.history(instance_id)
        active = service.active_run(instance_id)
        workflow_active = workflow_is_active(thread)
        visible_history = _through_latest_user(history) if workflow_active else history
        # What a conversation was asked to allow is part of the transcript, and
        # is loaded with it. The run stream replays these too, but it is only
        # opened for a run this process is still executing -- so a step that has
        # since finished, or one whose task this process no longer holds, used
        # to come back from a page load with its approvals missing entirely.
        approvals = await session.state_store.list_approvals(instance_id=instance_id)
        return JSONResponse(
            {
                "messages": _messages_json(visible_history),
                "approvals": [_approval_json(record) for record in approvals],
                # A complete assistant transcript can become durable just
                # before ActiveRun flips to done. In that window replaying it
                # would duplicate the assistant message in the client.
                "unstable_resume": workflow_active
                or (
                    active is not None
                    and bool(history)
                    and history[-1].role is Role.USER
                ),
            }
        )

    async def approval_events(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        return StreamingResponse(
            approval_feed.stream(instance_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def title_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        opening_text = str(body["text"]).strip() if body.get("text") else None
        runner = str(body["runner"]) if body.get("runner") else None
        try:
            title = await service.generate_title(instance_id, opening_text, runner)
        except ValueError as error:
            return _error(str(error), 400)
        except Exception as failure:
            # A provider that cannot name the chat has not cost anybody
            # anything yet, and must not be allowed to. The client asks for a
            # name *before* sending the message being named, so a failure
            # answered with a 500 here would take the user's turn with it --
            # a CLI that is out of quota would stop the chat working rather
            # than leave it called "New chat".
            #
            # The reason travels in the body instead of the status, because
            # something did go wrong and the placeholder name is not evidence
            # of which provider failed or why.
            return JSONResponse({"title": thread.title, "error": str(failure)})
        return JSONResponse({"title": title})

    async def attach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        try:
            thread = await service.attach_workspace(instance_id)
        except RuntimeError as error:
            # A repository that cannot produce a checkout -- unwired, or git
            # refusing -- is the server's problem to explain, not a 404.
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def detach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        # Workflow steps share one checkout. An earlier conversation may be
        # idle while a later step is still using that same directory.
        if workflow_is_active(thread):
            assert thread.workflow_run_id is not None
            state = await session.state_store.load(thread.workflow_run_id)
            if state is None or state.phase is RunPhase.RUNNING_AGENT:
                return _error("this workflow has a run in progress", 409)
        try:
            thread = await service.detach_workspace(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def run_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            text = _required_string(body, "text")
        except ValueError as error:
            return _error(str(error), 400)
        runner = str(body["runner"]) if body.get("runner") else None

        try:
            if thread.workflow_run_id is not None:
                if runner is not None and runner != thread.runner:
                    if runner not in workflow_executor.runners:
                        return _error(f"unknown workflow runner: {runner}", 400)
                    thread = await service.update_metadata(instance_id, runner=runner)
                await continue_workflow(thread, text)
                return StreamingResponse(
                    stream_workflow_conversation(instance_id, thread.workflow_run_id),
                    media_type="application/x-ndjson",
                )
            run = await service.start_run(instance_id, text, runner)
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        except RuntimeError as error:
            return _error(str(error), 409)

    async def resume_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        # Keep a completed snapshot available for the small race where history
        # observed an active run immediately before it finished.
        run = service.latest_run(instance_id)
        if run is not None:
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        if thread.workflow_run_id is not None:
            return StreamingResponse(
                stream_workflow_conversation(instance_id, thread.workflow_run_id),
                media_type="application/x-ndjson",
            )
        return Response(status_code=204)

    async def cancel_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        try:
            if thread.workflow_run_id is not None:
                await interrupt_workflow(thread)
            else:
                await service.stop_run(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return Response(status_code=204)

    async def decide_approval(request: Request) -> Response:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            workflow_agent_run_id = None
            if thread.workflow_run_id is not None:
                workflow_state = await session.state_store.load(thread.workflow_run_id)
                if workflow_state is not None:
                    workflow_agent_run_id = workflow_state.current_agent_run_id
            approval_id = ApprovalId(request.path_params["approval_id"])
            if "answers" in body:
                raw_answers = body["answers"]
                if not isinstance(raw_answers, dict):
                    raise ValueError("answers must be an object")
                answers = tuple(
                    UserInputAnswer(
                        question_id=str(question_id),
                        answers=tuple(values) if isinstance(values, list) else (),
                    )
                    for question_id, values in raw_answers.items()
                    if isinstance(question_id, str)
                    and isinstance(values, list)
                    and all(isinstance(value, str) for value in values)
                )
                if len(answers) != len(raw_answers):
                    raise ValueError("each answer must be an array of strings")
                approval = await service.answer_question(
                    instance_id, approval_id, answers, workflow_agent_run_id
                )
            else:
                decision = _required_string(body, "decision")
                approval = await service.decide_approval(
                    instance_id, approval_id, decision, workflow_agent_run_id
                )
        except ValueError as error:
            return _error(str(error), 400)
        except UnknownApprovalError as error:
            return _error(str(error), 404)
        except ApprovalDecisionNotAllowedError as error:
            return _error(str(error), 400)
        except UserInputNotAllowedError as error:
            return _error(str(error), 400)
        except ApprovalNotPendingError as error:
            # The request outlived whatever was waiting for it. Not the
            # client's mistake to fix by retrying, so not a 400.
            return _error(str(error), 409)
        return JSONResponse({"approval": _approval_json(approval)})

    # --- GitHub connection endpoints -----------------------------------------

    _credential_store = credential_store or GitHubCredentialStore()
    _source_control_preferences = (
        source_control_preferences or SourceControlPreferences()
    )

    # The single in-flight device flow. `_active_interval` tracks the current
    # polling interval, which grows when GitHub returns `slow_down`.
    _active_flow: DeviceFlowState | None = None
    _active_interval: int = 5

    def _is_local_request(request: Request) -> bool:
        """True when the request originates from the UI served by this process.

        The GitHub auth endpoints are mutating and must not be triggerable by
        arbitrary pages. Checking the Origin header against localhost is a
        lightweight CSRF guard appropriate for a local tool; it stops a
        cross-origin page from silently disconnecting the user's token or
        initiating a new device flow. GET /api/github/status is read-only and
        exempt.
        """
        origin = request.headers.get("origin", "")
        if not origin:
            # No Origin means a same-origin request (form submit, etc.) or a
            # curl call from localhost. Both are fine for a local tool.
            return True
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        # Browser Origin values have no path and lowercase hostnames.  Compare
        # parsed hosts rather than prefixes: localhost.evil.example is not
        # localhost.
        return parsed.hostname.lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
            (request.url.hostname or "").lower(),
        }

    def _hint(value: str) -> str:
        """Return first 4 chars + bullets so the UI can confirm which ID is set."""
        return value[:4] + "••••••••" if len(value) > 4 else "••••••••"

    def _effective_client_id() -> str:
        """Env-var takes precedence; keychain is the fallback for UI-configured IDs."""
        return github_client_id or _credential_store.get_client_id() or ""

    async def github_status(_request: Request) -> JSONResponse:
        credentials = _credential_store.get_credentials()
        now = time.time()
        connected = bool(
            credentials
            and (
                credentials.expires_at is None
                or credentials.expires_at > now
                or (
                    credentials.refresh_token is not None
                    and (
                        credentials.refresh_token_expires_at is None
                        or credentials.refresh_token_expires_at > now
                    )
                )
            )
        )
        return JSONResponse(
            {
                "connected": connected,
                "clientIdConfigured": bool(_effective_client_id()),
            }
        )

    async def source_control_status(_request: Request) -> JSONResponse:
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        cli = source_control_settings.gh_cli_status()
        return JSONResponse(
            {
                "provider": provider,
                "autoSelected": auto_selected,
                "ghCli": {
                    "installed": cli.installed,
                    "authenticated": cli.authenticated,
                    "account": cli.account,
                    "message": cli.message,
                },
            }
        )

    async def source_control_provider_status(_request: Request) -> JSONResponse:
        """Return the chosen provider without probing an unrelated CLI.

        A saved OAuth choice is a local settings-file read.  Do not make the
        Settings panel wait for ``gh auth status`` merely to render that choice.
        First-run auto-selection still performs its one required CLI probe.
        """
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        return JSONResponse(
            {"provider": provider, "autoSelected": auto_selected}
        )

    async def set_source_control_provider(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        provider = (await request.json()).get("provider")
        if provider == "gitlab":
            return _error("GitLab is not supported yet", 409)
        if provider not in {"gh-cli", "github-oauth"}:
            return _error("provider must be 'gh-cli' or 'github-oauth'", 400)
        _source_control_preferences.set(provider)
        return Response(status_code=204)

    async def github_get_client_id(_request: Request) -> JSONResponse:
        # Never return the actual value — only whether one is set and its hint.
        stored = _credential_store.get_client_id()
        if github_client_id:
            return JSONResponse(
                {"source": github_client_id_source, "hint": _hint(github_client_id)}
            )
        if stored:
            return JSONResponse({"source": "keychain", "hint": _hint(stored)})
        return JSONResponse({"source": "none", "hint": ""})

    async def github_set_client_id(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        if not client_id:
            return _error("clientId is required", 400)
        try:
            _credential_store.set_client_id(client_id)
        except GitHubAuthError as error:
            return _error(str(error), 500)
        return Response(status_code=204)

    async def github_connect(request: Request) -> JSONResponse:
        nonlocal _active_flow, _active_interval
        if not _is_local_request(request):
            return _error("forbidden", 403)
        effective_client_id = _effective_client_id()
        if not effective_client_id:
            return _error(
                "GitHub client ID is not configured. Enter it in Settings.", 503
            )
        # Return the in-flight flow rather than discarding it — a second tab
        # or a retry gets the same codes instead of racing with any polling
        # that is still running against the first flow.
        if _active_flow is not None:
            return JSONResponse(
                {
                    "userCode": _active_flow.user_code,
                    "verificationUri": _active_flow.verification_uri,
                    "expiresIn": _active_flow.expires_in,
                    "interval": _active_interval,
                }
            )
        try:
            _active_flow = await start_device_flow(effective_client_id)
        except GitHubAuthError as error:
            return _error(str(error), 502)
        _active_interval = _active_flow.interval
        return JSONResponse(
            {
                "userCode": _active_flow.user_code,
                "verificationUri": _active_flow.verification_uri,
                "expiresIn": _active_flow.expires_in,
                "interval": _active_interval,
            }
        )

    async def github_connect_poll(request: Request) -> JSONResponse:
        nonlocal _active_flow, _active_interval
        if not _is_local_request(request):
            return _error("forbidden", 403)
        if _active_flow is None:
            return _error(
                "no active device flow; call POST /api/github/connect first", 409
            )
        try:
            result = await poll_device_flow(
                _effective_client_id(), _active_flow.device_code, _active_interval
            )
        except GitHubAuthError as error:
            _active_flow = None
            return _error(str(error), 502)
        if isinstance(result, DeviceFlowComplete):
            try:
                _credential_store.set_credentials(credentials_from_device_flow(result))
            except GitHubAuthError as error:
                _active_flow = None
                return _error(str(error), 500)
            _active_flow = None
            return JSONResponse({"status": "complete"})
        # DeviceFlowPending — update the interval in case GitHub slowed us down.
        _active_interval = result.next_interval
        return JSONResponse({"status": "pending", "nextInterval": _active_interval})

    async def github_disconnect(request: Request) -> Response:
        nonlocal _active_flow
        if not _is_local_request(request):
            return _error("forbidden", 403)
        _active_flow = None
        _credential_store.delete()
        return Response(status_code=204)

    async def graph_surface(scope: Scope, receive: Receive, send: Send) -> None:
        """Pass anything under `/graph` to the graph engine's own server.

        The graph engine ships a small API of its own -- what a run is doing,
        what it has raised, and the two things a person can send back: a
        message for whichever agent is working, and an answer to a question it
        stopped on. That is how a `[BETA]` run gets approved today, and this
        app's pages cannot do it yet.

        A hop rather than a re-implementation, and behind a prefix of its own
        because both servers call their runs `/api/runs`. It has to be a
        forwarder rather than a plain mount because the engine on the far side
        does not exist until the server starts.
        """
        if surface.app is None:
            await JSONResponse(
                {"error": "this process is not running graph workflows"},
                status_code=503,
            )(scope, receive, send)
            return
        await surface.app(scope, receive, send)

    # --- Slack connection endpoints ------------------------------------------

    _slack_store = slack_credential_store or SlackCredentialStore()
    _slack_state: str | None = None
    _slack_redirect_uri: str | None = None
    # The way back into a chat thread, for the one message this app sends
    # itself: the reply that says a mention became a work order. Everything
    # after that is the executor's, which builds its own from the same port.
    run_notifier = RunNotifier(session.capabilities.communications, public_url)

    def _signing_secret() -> str:
        return _slack_store.signing_secret() or ""

    async def slack_status(_request: Request) -> JSONResponse:
        credentials = _slack_store.credentials()
        return JSONResponse(
            {
                "configured": credentials is not None,
                "connected": bool(_slack_store.token()),
                # Whether a mention could actually start something: the two
                # halves are independent, and a deployment that connected but
                # never saved a signing secret hears nothing.
                "events": bool(_signing_secret()) and bool(work_orders.repository),
            }
        )

    async def slack_set_credentials(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        client_secret = (body.get("clientSecret") or "").strip()
        signing_secret = (body.get("signingSecret") or "").strip()
        if not client_id or not client_secret:
            return _error("clientId and clientSecret are required", 400)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
            _slack_store.disconnect()
        try:
            _slack_store.set_credentials(client_id, client_secret)
            if signing_secret:
                # After the credentials, never before: saving them forgets the
                # previous app's signing secret, which would take this one too.
                _slack_store.set_signing_secret(signing_secret)
        except SlackAuthError as error:
            return _error(str(error), 500)
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    async def slack_connect(request: Request) -> JSONResponse:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        credentials = _slack_store.credentials()
        if credentials is None:
            return _error("Slack OAuth credentials are not configured", 503)
        _slack_state = uuid4().hex
        _slack_redirect_uri = str(request.url_for("slack_callback"))
        return JSONResponse(
            {"authorizationUrl": slack_authorization_url(credentials.client_id, _slack_redirect_uri, _slack_state)}
        )

    async def slack_callback(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _slack_state or request.query_params.get("state") != _slack_state:
            return _error("invalid OAuth state", 400)
        code = request.query_params.get("code")
        credentials = _slack_store.credentials()
        if not code or credentials is None or _slack_redirect_uri is None:
            return _error(request.query_params.get("error", "authorization was not completed"), 400)
        try:
            token = await exchange_slack_code(credentials, code, _slack_redirect_uri)
            _slack_store.set_token(token)
        except SlackAuthError as error:
            return _error(str(error), 502)
        finally:
            _slack_state = None
            _slack_redirect_uri = None
        return Response(
            "<html><body><p>Slack connected. You can close this window.</p>"
            "<script>window.close()</script></body></html>",
            media_type="text/html",
        )

    async def slack_disconnect(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
        _slack_store.disconnect()
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    async def slack_events(request: Request) -> Response:
        """Slack's Events API: the door a mention comes in through.

        Every answer here is a 200 with an empty body once the delivery is
        established as Slack's, including the ones where nothing happens. Slack
        reads any other status as "did not arrive" and sends it again, so a
        work order that failed to start for a reason retrying cannot fix would
        be attempted three more times -- and one that started successfully but
        answered slowly would be started twice.

        A signature that does not verify is the exception, and is refused: an
        unsigned request to this address is not Slack, and starting agents on
        the say-so of whoever found the URL is the one thing this must not do.
        """
        body = await request.body()
        signing_secret = _signing_secret()
        if not signing_secret:
            log.warning(
                "a Slack event was delivered but no signing secret is saved, "
                "so it could not be verified and was ignored"
            )
            return _error("Slack request signing is not configured", 503)
        if not verify_slack_signature(
            signing_secret,
            request.headers.get("x-slack-request-timestamp", ""),
            request.headers.get("x-slack-signature", ""),
            body,
        ):
            return _error("invalid Slack signature", 401)
        try:
            payload = json.loads(body)
        except ValueError:
            return _error("invalid Slack event", 400)
        if not isinstance(payload, dict):
            return _error("invalid Slack event", 400)
        if payload.get("type") == "url_verification":
            # The one-off handshake that makes Slack accept this address.
            return JSONResponse({"challenge": str(payload.get("challenge", ""))})
        if request.headers.get("x-slack-retry-num"):
            # A redelivery of something already accepted. Whatever it was, it
            # is either running or already failed for a reason nothing about
            # this attempt changes; acting again would double the work order.
            return Response(status_code=200)
        mention = slack_mention_from_event(payload)
        if mention is not None:
            await start_mentioned_work_order(mention)
        return Response(status_code=200)

    async def start_mentioned_work_order(mention: SlackMention) -> None:
        """Turn somebody pinging the bot into a work order, and say so.

        Only the step workflows are startable this way. A `[BETA]` graph run is
        driven by the other engine, which has neither the run-bound tools an
        agent reports status through nor a place to keep where the request came
        from -- so offering one here would be offering a work order that goes
        silent the moment it starts.
        """
        origin = RunOrigin(
            channel=mention.channel,
            thread_id=mention.thread_id,
            author=mention.author,
        )

        async def refuse(reason: str) -> None:
            await run_notifier.post(
                origin, CommunicationsMessage(reason, mention=origin.author)
            )

        if not work_orders.repository:
            await refuse(
                "I cannot start a work order until this deployment configures "
                "`work_orders.repository`."
            )
            return
        definition = _mentioned_workflow()
        if definition is None:
            await refuse(
                "I cannot start a work order: this deployment has no step "
                "workflow configured under `work_orders.workflow`."
            )
            return
        runner_name = work_orders.runner or workflow_executor.default_runner
        if runner_name not in workflow_executor.runners:
            await refuse(f"I cannot start a work order: unknown runner {runner_name}.")
            return
        state = await start_step_run(
            prompt=mention.text,
            repository=work_orders.repository,
            workflow_id=definition.workflow_id,
            definition=definition,
            runner_name=runner_name,
            origin=origin,
        )
        link = run_notifier.work_order_link(state)
        await run_notifier.post(
            origin,
            CommunicationsMessage(
                f"Started a work order on `{work_orders.repository}`. "
                "I will report progress here.",
                (link,) if link is not None else (),
                mention=origin.author,
            ),
            state,
        )

    def _mentioned_workflow() -> WorkflowDefinition | None:
        """Which workflow a mention runs: the configured one, or the only one."""
        if work_orders.workflow:
            return catalog.get(WorkflowId(work_orders.workflow))
        return next(iter(catalog)) if len(catalog) == 1 else None

    routes = [
        Route("/api/config", config),
        Route("/api/github/status", github_status),
        Route("/api/source-control/status", source_control_status),
        Route("/api/source-control/provider", source_control_provider_status),
        Route(
            "/api/source-control/provider",
            set_source_control_provider,
            methods=["POST"],
        ),
        Route("/api/github/client-id", github_get_client_id),
        Route("/api/github/client-id", github_set_client_id, methods=["POST"]),
        Route("/api/github/connect", github_connect, methods=["POST"]),
        Route("/api/github/connect/poll", github_connect_poll, methods=["POST"]),
        Route("/api/github/disconnect", github_disconnect, methods=["POST"]),
        Route("/api/slack/status", slack_status),
        Route("/api/slack/credentials", slack_set_credentials, methods=["POST"]),
        Route("/api/slack/connect", slack_connect, methods=["POST"]),
        Route("/api/slack/callback", slack_callback, name="slack_callback"),
        Route("/api/slack/disconnect", slack_disconnect, methods=["POST"]),
        Route("/api/slack/events", slack_events, methods=["POST"]),
        Route("/api/projects", list_projects),
        Route("/api/projects", create_project, methods=["POST"]),
        Route(
            "/api/projects/{project_id}/archive",
            archive_project,
            methods=["POST"],
            name="archive_project",
        ),
        Route(
            "/api/projects/{project_id}/unarchive",
            archive_project,
            methods=["POST"],
            name="unarchive_project",
        ),
        Route("/api/projects/{project_id}/milestones", list_project_milestones),
        Route("/api/runs", list_runs),
        Route("/api/runs", create_run, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run),
        Route("/api/runs/{run_id}", delete_run, methods=["DELETE"]),
        Route("/api/runs/{run_id}/graph-events", graph_run_events),
        Route(
            "/api/runs/{run_id}/human-review",
            complete_human_review,
            methods=["POST"],
        ),
        # The `[BETA]` half of the runs above, served by the engine that runs
        # them rather than by this file.
        Mount(GRAPH_PREFIX, app=graph_surface),
        Route("/api/threads", list_threads),
        Route("/api/threads", create_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}", get_thread),
        Route("/api/threads/{thread_id}", update_thread, methods=["PATCH"]),
        Route("/api/threads/{thread_id}", delete_thread, methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/archive",
            archive_thread,
            methods=["POST"],
            name="archive",
        ),
        Route(
            "/api/threads/{thread_id}/unarchive",
            archive_thread,
            methods=["POST"],
            name="unarchive",
        ),
        Route("/api/threads/{thread_id}/messages", messages),
        Route("/api/threads/{thread_id}/approval-events", approval_events),
        Route(
            "/api/threads/{thread_id}/workspace",
            attach_workspace,
            methods=["POST"],
        ),
        Route(
            "/api/threads/{thread_id}/workspace",
            detach_workspace,
            methods=["DELETE"],
        ),
        Route("/api/threads/{thread_id}/title", title_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}/runs", run_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}/runs/current", resume_run),
        Route("/api/threads/{thread_id}/runs/current", cancel_run, methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/runs/current/approvals/{approval_id}",
            decide_approval,
            methods=["POST"],
        ),
    ]
    if static_directory is not None and (static_directory / "index.html").is_file():

        async def spa_page(_request: Request) -> Response:
            return FileResponse(
                static_directory / "index.html",
                headers={"cache-control": "no-cache"},
            )

        routes.extend(
            [
                Route("/runs", spa_page),
                Route("/runs/new", spa_page),
                Route("/runs/{run_id}/conversations/{thread_id}", spa_page),
                Route("/runs/{run_id}", spa_page),
                Route("/conversations", spa_page),
                Route("/conversations/{thread_id}", spa_page),
                Route("/plan", spa_page),
                Route("/projects/{project_id}/milestones", spa_page),
                Route("/projects/{project_id}/milestones/{milestone_id}", spa_page),
                Route(
                    "/projects/{project_id}/milestones/{milestone_id}/tasks/new",
                    spa_page,
                ),
            ]
        )
        routes.append(Mount("/", BuiltClient(directory=static_directory, html=True)))
    else:
        routes.append(Route("/", _missing_frontend))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.thread_service = service
    return app


def _with_workspace(thread: ChatThread, state: WorkspaceState | None) -> ChatThread:
    """Fold a provider's answer into the thread the UI is shown."""
    thread.workspace_id = state.workspace_id if state is not None else None
    thread.workspace_ref = state.ref if state is not None else None
    thread.workspace_root = state.root_path if state is not None else None
    return thread


def _thread_json(thread: ChatThread) -> dict[str, object]:
    result: dict[str, object] = {
        "id": str(thread.instance_id),
        "title": thread.title,
        "archived": thread.archived,
        "agentId": str(thread.agent_id),
        "runner": thread.runner,
        # Present but detached is a state of its own: the work is still there,
        # on the ref, and attaching brings a checkout back to it.
        "workspaceAttached": thread.workspace_root is not None,
    }
    if thread.workspace_root is not None:
        result["workspaceRoot"] = thread.workspace_root
    if thread.workspace_ref is not None:
        result["workspaceRef"] = thread.workspace_ref
    if thread.workflow_run_id is not None:
        result["workflowRunId"] = str(thread.workflow_run_id)
    if thread.workflow_step_id is not None:
        result["workflowStepId"] = str(thread.workflow_step_id)
        result["editable"] = thread.editable
        result["autoApprove"] = thread.auto_approve
    return result


def _project_json(
    project: Project,
    conversations: Container[AgentInstanceId],
    *,
    milestones: int | None = None,
) -> dict[str, object]:
    """Render a project, linked to its plan when that conversation is open.

    `conversations` is required rather than defaulted: a caller that forgot it
    would quietly emit the linkless rows this link exists to replace.

    `milestones` is how many the project has, for the callers that counted
    them. Left out rather than reported as none by the ones that did not: a
    zero here is a project with no plan, which is a thing the rail acts on.
    """

    result: dict[str, object] = {
        "projectId": str(project.project_id),
        "name": project.name,
        "archived": project.archived,
    }
    if milestones is not None:
        result["milestoneCount"] = milestones
    instance_id = instance_id_for_project(project.project_id)
    if instance_id is not None and instance_id in conversations:
        result["conversationUrl"] = f"/conversations/{quote(str(instance_id), safe='')}"
    return result


def _milestone_json(
    milestone: Milestone, workstreams: Sequence[Workstream]
) -> dict[str, object]:
    return {
        "milestoneId": str(milestone.milestone_id),
        "name": milestone.name,
        "description": milestone.description,
        "dependencies": [str(dependency) for dependency in milestone.dependencies],
        "workstreams": [_workstream_json(workstream) for workstream in workstreams],
    }


def _workstream_json(workstream: Workstream) -> dict[str, object]:
    return {
        "workstreamId": str(workstream.workstream_id),
        "name": workstream.name,
        "scope": workstream.scope,
    }


def _workflow_step_editable(
    definition: WorkflowDefinition | None, step_id: StepId | None
) -> bool:
    """Resolve UI behavior from the run's compiled workflow snapshot."""

    if definition is None or step_id is None:
        return False
    step = definition.step(step_id)
    return isinstance(step, AgentStep) and step.editable


def _run_json(run: WorkflowRunView, *, listing: bool = False) -> dict[str, object]:
    """One WorkOrder, as a client is shown it.

    A listing leaves out the prose an agent wrote: step summaries and outputs,
    the task prompt, a failure's reason, and the review and decision bodies.
    Every screen polls `/api/runs` once a second to keep the rail current, so
    what that list carries is what every screen pays for, on a payload that
    grows with the transcript of every run ever started. The pages that draw
    the prose read the one run they are about from `/api/runs/{run_id}`.
    """
    steps: list[dict[str, object]] = [
        {
            "stepId": str(step.step_id),
            "name": step.name,
            "kind": step.kind,
            "status": step.status,
            "outcome": step.outcome,
            "changesRequested": step.changes_requested,
            "agentId": str(step.agent_id) if step.agent_id else None,
            "agentInstanceId": (
                str(step.agent_instance_id) if step.agent_instance_id else None
            ),
            "agentRunId": str(step.agent_run_id) if step.agent_run_id else None,
            "mcpRequestId": step.mcp_request_id,
            "conversationId": (
                str(step.conversation_id) if step.conversation_id else None
            ),
            "conversationUrl": (
                f"/runs/{run.run_id}/conversations/{step.agent_instance_id}"
                if step.agent_instance_id
                else None
            ),
            "waiting": step.waiting,
        }
        for step in run.steps
    ]
    result: dict[str, object] = {
        "runId": str(run.run_id),
        "name": run.name,
        "workflowId": run.workflow_id,
        "workflowName": run.workflow_name,
        "workflowVersion": run.workflow_version,
        "taskId": run.task_id,
        "workstreamId": str(run.workstream_id) if run.workstream_id else None,
        "milestoneId": str(run.milestone_id) if run.milestone_id else None,
        "repository": run.repository,
        "repositoryContext": {"repository": run.repository},
        "phase": run.phase,
        "currentStepId": str(run.current_step_id) if run.current_step_id else None,
        "terminalOutcome": run.terminal_outcome,
        "steps": steps,
    }
    if listing:
        return result
    result["taskPrompt"] = run.task_prompt
    result["failureReason"] = run.failure_reason
    for step, step_json in zip(run.steps, steps):
        step_json["summary"] = step.summary
        step_json["outputs"] = [
            {"name": output.name, "value": output.value} for output in step.outputs
        ]
    if run.pending_human_review is not None:
        result["pendingHumanReview"] = {
            "stepId": str(run.pending_human_review.step_id),
            "title": run.pending_human_review.title,
            "summary": run.pending_human_review.summary,
            "prUrl": run.pending_human_review.pull_request_url,
        }
    else:
        result["pendingHumanReview"] = None
    if run.human_decision is not None:
        result["humanDecision"] = {
            "stepId": str(run.human_decision.step_id),
            "approved": run.human_decision.approved,
            "outcome": run.human_decision.outcome,
            "summary": run.human_decision.summary,
        }
    else:
        result["humanDecision"] = None
    return result


def _approval_json(approval: ApprovalRecord) -> dict[str, object]:
    """One complete request, as the client is shown it.

    Whole rather than incremental, like the content snapshots beside it: a
    client that reconnected mid-pause has no way to reconstruct a request from
    the parts of it that were emitted before it arrived.
    """
    result = {
        "id": str(approval.approval_id),
        "status": approval.status.value,
        "kind": approval.kind.value,
        "reason": approval.reason,
        "command": approval.command,
        "cwd": approval.cwd,
        "toolName": approval.tool_name,
        # The call this was asked about, so the client can show the request
        # beside it rather than collecting every request at the end of a turn.
        "toolCallId": approval.tool_call_id,
        "arguments": approval.arguments,
        "allowedDecisions": [decision.value for decision in approval.allowed_decisions],
        "decision": approval.decision.value if approval.decision else None,
        # Who decided, so the client can tell an answer the user gave from one
        # a grant gave on their behalf. A request nobody was shown still has to
        # read as something that happened, not as something that was skipped.
        "decisionSource": (
            approval.decision_source.value if approval.decision_source else None
        ),
    }
    if approval.questions is not None:
        result["questions"] = _json_value(approval.questions, [])
    if approval.answers is not None:
        result["answers"] = _json_value(approval.answers, None)
    return result


def _json_value(value: str | None, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _messages_json(messages: tuple[Message, ...]) -> list[dict[str, object]]:
    """Group the engine's turn transcript into assistant-ui messages."""
    result: list[dict[str, object]] = []
    assistant_content: list[dict[str, object]] = []
    assistant_id = ""
    tool_calls: dict[str, dict[str, object]] = {}

    def flush_assistant() -> None:
        nonlocal assistant_content, assistant_id
        if assistant_content:
            result.append(
                {
                    "id": assistant_id or f"assistant-{len(result)}",
                    "role": Role.ASSISTANT.value,
                    "content": assistant_content,
                }
            )
        assistant_content = []
        assistant_id = ""

    for index, message in enumerate(messages):
        if message.role is Role.USER:
            flush_assistant()
            if message.content:
                result.append(
                    {
                        "id": str(message.message_id or f"user-{index}"),
                        "role": Role.USER.value,
                        "content": [{"type": "text", "text": message.content}],
                    }
                )
            continue
        if not assistant_id and message.message_id:
            assistant_id = str(message.message_id)
        _merge_message(assistant_content, message, tool_calls)
    flush_assistant()
    return result


def _through_latest_user(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Hide an in-flight assistant transcript that resume will stream anew."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role is Role.USER:
            return messages[: index + 1]
    return messages


def _latest_assistant_content(
    messages: tuple[Message, ...],
) -> list[dict[str, object]]:
    """Build the current assistant snapshot after the latest user message."""
    suffix_start = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role is Role.USER:
            suffix_start = index + 1
            break

    content: list[dict[str, object]] = []
    tool_calls: dict[str, dict[str, object]] = {
        call_id: {} for call_id in _tool_call_ids(messages[:suffix_start])
    }
    for message in messages[suffix_start:]:
        _merge_message(content, message, tool_calls)
    return content


def _tool_call_ids(messages: Iterable[Message]) -> set[str]:
    """Every provider call id already present in a transcript."""
    return {call.call_id for message in messages for call in message.tool_calls}


def _merge_message(
    content: list[dict[str, object]],
    message: Message,
    tool_calls: dict[str, dict[str, object]] | None = None,
) -> bool:
    """Fold one engine message into one assistant-ui assistant response."""
    if tool_calls is None:
        tool_calls = {
            str(part["toolCallId"]): part
            for part in content
            if part.get("type") == "tool-call" and "toolCallId" in part
        }
    changed = False
    if message.role is Role.ASSISTANT:
        if message.content:
            content.append({"type": "text", "text": message.content})
            changed = True
        for call in message.tool_calls:
            # Provider streams and resumed sessions can replay the same item.
            # It is one call semantically, and assistant-ui requires it to be
            # one resource structurally, so retain the first occurrence.
            if call.call_id in tool_calls:
                continue
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            part: dict[str, object] = {
                "type": "tool-call",
                "toolCallId": call.call_id,
                "toolName": call.name,
                "args": arguments,
                "argsText": call.arguments,
            }
            content.append(part)
            tool_calls[call.call_id] = part
            clarification = _clarification_context(call.name, arguments)
            if clarification:
                content.append({"type": "text", "text": clarification})
            changed = True
    elif message.role is Role.TOOL and message.tool_call_id:
        part = tool_calls.get(message.tool_call_id)
        if part is not None:
            part["result"] = message.content
            changed = any(candidate is part for candidate in content)
    return changed


_CLARIFICATION_TOOLS = frozenset(
    {
        "askuserquestion",
        "escalate",
        "escalatetohuman",
        "requestclarification",
        "requesthumanreview",
        "requestuserinput",
    }
)


def _clarification_context(tool_name: str, arguments: object) -> str | None:
    """Extract the question text from a provider's clarification tool call."""

    leaf_name = tool_name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    normalized = "".join(
        character for character in leaf_name.lower() if character.isalnum()
    )
    if normalized not in _CLARIFICATION_TOOLS or not isinstance(arguments, dict):
        return None

    questions = arguments.get("questions")
    candidates = questions if isinstance(questions, list) else (arguments,)
    context: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            text = candidate.strip()
        elif isinstance(candidate, dict):
            text = next(
                (
                    value.strip()
                    for key in ("question", "prompt", "message")
                    if isinstance((value := candidate.get(key)), str) and value.strip()
                ),
                "",
            )
        else:
            text = ""
        if text and text not in context:
            context.append(text)
    return "\n\n".join(context) or None


_TITLE_PROMPT = (
    "Name this chat based on the conversation above. Reply with only a concise "
    "title of at most eight words, with no quotes or ending punctuation."
)


def _clean_title(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    return first_line.strip(" \t\"'`).:;!?")[:80]


async def _json_body(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _required_string(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, object], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _thread_id(request: Request) -> AgentInstanceId:
    return AgentInstanceId(request.path_params["thread_id"])


def _new_agent_run_id() -> AgentRunId:
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _server_event(value: dict[str, object]) -> bytes:
    return f"data:{json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _missing_frontend(_request: Request) -> Response:
    return Response(
        "The assistant-ui client has not been built. Run `npm --prefix apps/web run build`.",
        status_code=503,
        media_type="text/plain",
    )


__all__ = ["ChatThread", "ThreadService", "create_app"]
