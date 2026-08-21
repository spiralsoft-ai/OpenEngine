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
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from engine.core.workflows.implementation_review import (
    IMPLEMENTATION_STEP_SPEC,
    REVIEW_STEP_SPEC,
    WORKFLOW_BASE_REF,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    HumanReviewCompleted,
    IMPLEMENTATION_REVIEW_WORKFLOW_ID,
    Message,
    Role,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.ports import (
    AgentRunner,
    ApprovalHandler,
    InteractiveAgentRunner,
    StateStore,
    UserInputAnswer,
    WorkspaceState,
)
from engine.runtime import (
    AgentSession,
    ApprovalBroker,
    ApprovalConfig,
    ApprovalDecisionNotAllowedError,
    ApprovalNotPendingError,
    RunReader,
    UnknownApprovalError,
    UserInputNotAllowedError,
    WorkflowExecutionError,
    WorkflowExecutor,
    WorkflowRunView,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


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

    def __init__(self, agent_run_id: AgentRunId) -> None:
        self.agent_run_id = agent_run_id
        self.content: list[dict[str, object]] = []
        self.approvals: dict[str, dict[str, object]] = {}
        """The latest snapshot of every request this run has raised, by id.

        A map rather than "the one the turn is on", because what a subscriber
        needs is each request's *transition*: a turn let go by a decision often
        asks its next question before anyone has been told about the answer, and
        a single slot would hand the new question over in place of it -- leaving
        a card waiting forever on a request that was decided.
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
        sent: dict[str, dict[str, object]] = {}
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                approvals = {key: dict(value) for key, value in self.approvals.items()}
                error = self.error
                done = self.done

            for approval_id, approval in approvals.items():
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
        if not _merge_message(self.content, message):
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
            self._revision += 1
            self._changed.notify_all()

    def note_approval(self, approval: ApprovalRecord) -> None:
        """Update the snapshot without waking anyone, for a run that is ending.

        Synchronous on purpose. The wake that matters is the one the run's own
        ending sends a moment later, and awaiting a lock here would yield the
        event loop back to the very turn being torn down.
        """
        self.approvals[str(approval.approval_id)] = _approval_json(approval)

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
        condition = self._changed.setdefault(
            approval.instance_id, asyncio.Condition()
        )
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
            title=instance.title,
            archived=instance.archived,
            workflow_run_id=instance.workflow_run_id,
            workflow_step_id=instance.workflow_step_id,
            editable=_workflow_step_editable(instance.workflow_step_id),
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
                raise RuntimeError("workflow run not found")
            repository = run.repository
            base_ref = WORKFLOW_BASE_REF
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
        initial_message_count = len(await self.session.history(instance_id))
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        # Named before it starts, because the approvals it raises are brokered
        # against this run and a decision has to be able to name it too.
        agent_run_id = _new_agent_run_id()
        selected_runner = runner or thread.runner
        run = ActiveRun(agent_run_id)
        self._active_runs[instance_id] = run
        on_approval = None
        selected = self._runners.get(selected_runner)
        if isinstance(selected, InteractiveAgentRunner):
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
        if thread.title != "New chat":
            return thread.title
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        async with self._locks[instance_id]:
            if thread.title != "New chat":
                return thread.title
            history = await self.session.history(instance_id)
            title_context = (
                (*history, Message.user(opening_text)) if opening_text else history
            )
            turn = await self._runners[selected_runner].run_turn(
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
            if thread.workflow_step_id not in {
                IMPLEMENTATION_STEP_SPEC.step_id,
                REVIEW_STEP_SPEC.step_id,
            }:
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
            return _with_workspace(thread, await self.session.workspace(thread.instance_id))
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
                    title=instance.title,
                    archived=instance.archived,
                    workflow_run_id=instance.workflow_run_id,
                    workflow_step_id=instance.workflow_step_id,
                    editable=_workflow_step_editable(instance.workflow_step_id),
                    auto_approve=instance.auto_approve,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            # A CLI subprocess does not survive the server that spawned it, so
            # a request still marked pending here was asked by a process that
            # no longer exists and can never be answered.
            await self.approvals.interrupt_orphans()
            self._restored = True


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
    *,
    workflow_runners: Mapping[str, AgentRunner] | None = None,
    review_runners: Mapping[str, AgentRunner] | None = None,
    approval_policy: ApprovalConfig = ApprovalConfig(),
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    if workflow_runners is not None and review_runners is None:
        raise ValueError("review_runners are required with workflow_runners")
    approval_feed = ApprovalFeed(session.state_store)
    service = ThreadService(
        session,
        runners,
        approval_policy,
        approval_observer=approval_feed.publish,
    )
    run_reader = RunReader(session.state_store)

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
        )

    workflow_executor = WorkflowExecutor(
        session.capabilities,
        workflow_runners if workflow_runners is not None else runners,
        review_runners=review_runners if review_runners is not None else runners,
        approval_handler=workflow_approval_handler,
    )
    workflow_tasks: dict[RunId, asyncio.Task[None]] = {}

    def track_workflow(run_id: RunId, task: asyncio.Task[None]) -> None:
        workflow_tasks[run_id] = task
        task.add_done_callback(
            lambda completed: workflow_tasks.pop(run_id, None)
            if workflow_tasks.get(run_id) is completed
            else None
        )

    async def restore_reviews() -> None:
        """Restart review commands whose process-local dispatch was lost."""
        for state in await session.state_store.list_runs():
            if state.phase is not RunPhase.REVIEWING or state.run_id in workflow_tasks:
                continue
            instances = await session.state_store.list_instances(
                workflow_run_id=state.run_id
            )
            implementation_step_id = (
                state.step_results[-1].step_id if state.step_results else None
            )
            implementation = next(
                (
                    instance
                    for instance in instances
                    if instance.workflow_step_id == implementation_step_id
                ),
                None,
            )
            runner_name = (
                implementation.runner
                if implementation is not None and implementation.runner
                else workflow_executor.default_runner
            )
            track_workflow(
                state.run_id,
                asyncio.create_task(
                    workflow_executor.resume_review(state.run_id, runner_name)
                ),
            )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await restore_reviews()
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
            or state.phase is not RunPhase.IMPLEMENTING
            or state.current_step_id != thread.workflow_step_id
        ):
            raise RuntimeError("this workflow step is no longer active")
        if state.current_agent_run_id is not None:
            await service.approvals.cancel_run(state.current_agent_run_id)
        task = workflow_tasks.get(thread.workflow_run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
        task = asyncio.create_task(
            workflow_executor.resume_implementation(
                thread.workflow_run_id, text, thread.runner
            )
        )
        track_workflow(thread.workflow_run_id, task)
        while (
            len(await service.history(thread.instance_id)) <= before
            and not task.done()
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
                "defaultRunner": session.default_runner,
                "workflowRunners": list(workflow_executor.runners),
                "defaultWorkflowRunner": workflow_executor.default_runner,
            }
        )

    async def list_threads(_request: Request) -> JSONResponse:
        return JSONResponse({"threads": [_thread_json(t) for t in await service.list()]})

    async def list_runs(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"runs": [_run_json(run) for run in await run_reader.list()]}
        )

    async def create_run(request: Request) -> JSONResponse:
        """Persist a workflow request and start its supported local execution."""
        body = await _json_body(request)
        try:
            prompt = _required_string(body, "prompt")
            repository = _required_string(body, "repository")
            workflow_id = WorkflowId(_required_string(body, "workflowId"))
        except ValueError as error:
            return _error(str(error), 400)
        if workflow_id != IMPLEMENTATION_REVIEW_WORKFLOW_ID:
            return _error(f"unknown workflow definition: {workflow_id}", 400)
        runner_name = str(body.get("runner") or workflow_executor.default_runner)
        if runner_name not in workflow_executor.runners:
            return _error(f"unknown workflow runner: {runner_name}", 400)

        run_id = RunId(f"run-{uuid4().hex[:12]}")
        task_id = TaskId(f"task-{uuid4().hex[:12]}")
        event = RunRequested(
            run_id=run_id,
            task_id=task_id,
            prompt=prompt,
            repository=repository,
            workflow_id=workflow_id,
        )
        state = RunState(
            run_id=run_id,
            task_id=task_id,
            workflow_id=workflow_id,
            prompt=prompt,
            repository=repository,
        )
        await session.state_store.save(state)
        await session.state_store.append_events(run_id, (event,))
        track_workflow(
            run_id,
            asyncio.create_task(
                workflow_executor.advance_through_review(event, runner_name)
            ),
        )
        run = await run_reader.get(run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def get_run(request: Request) -> JSONResponse:
        run = await run_reader.get(RunId(request.path_params["run_id"]))
        if run is None:
            return _error("run not found", 404)
        return JSONResponse(_run_json(run))

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
            await workflow_executor.complete_human_review(
                HumanReviewCompleted(
                    run_id=run_id,
                    step_id=state.current_step_id,
                    approved=approved,
                    summary=summary,
                )
            )
        except WorkflowExecutionError as error:
            return _error(str(error), 409)
        run = await run_reader.get(run_id)
        assert run is not None
        return JSONResponse(_run_json(run))

    async def create_thread(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            thread = await service.create(
                AgentId(_required_string(body, "agentId")),
                _required_string(body, "runner"),
            )
        except (KeyError, ValueError) as error:
            return _error(str(error), 400)
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
                    return _error("a workflow run chooses its runner", 400)
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
                workflow_state = await session.state_store.load(
                    thread.workflow_run_id
                )
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

    routes = [
        Route("/api/config", config),
        Route("/api/runs", list_runs),
        Route("/api/runs", create_run, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run),
        Route(
            "/api/runs/{run_id}/human-review",
            complete_human_review,
            methods=["POST"],
        ),
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
        if thread.workflow_step_id in {
            IMPLEMENTATION_STEP_SPEC.step_id,
            REVIEW_STEP_SPEC.step_id,
        }:
            result["autoApprove"] = thread.auto_approve
    return result


def _workflow_step_editable(step_id: StepId | None) -> bool:
    """Resolve UI behavior from the built-in workflow's step configuration."""

    return any(
        step.step_id == step_id and step.editable
        for step in (IMPLEMENTATION_STEP_SPEC, REVIEW_STEP_SPEC)
    )


def _run_json(run: WorkflowRunView) -> dict[str, object]:
    result: dict[str, object] = {
        "runId": str(run.run_id),
        "name": run.name,
        "workflowId": run.workflow_id,
        "workflowName": run.workflow_name,
        "workflowVersion": run.workflow_version,
        "taskId": run.task_id,
        "taskPrompt": run.task_prompt,
        "repository": run.repository,
        "repositoryContext": {"repository": run.repository},
        "phase": run.phase,
        "currentStepId": str(run.current_step_id) if run.current_step_id else None,
        "terminalOutcome": run.terminal_outcome,
        "failureReason": run.failure_reason,
        "steps": [
            {
                "stepId": str(step.step_id),
                "name": step.name,
                "kind": step.kind,
                "status": step.status,
                "outcome": step.outcome,
                "summary": step.summary,
                "outputs": [
                    {"name": output.name, "value": output.value}
                    for output in step.outputs
                ],
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
        ],
    }
    if run.pending_human_review is not None:
        result["pendingHumanReview"] = {
            "stepId": str(run.pending_human_review.step_id),
            "title": run.pending_human_review.title,
            "summary": run.pending_human_review.summary,
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
        "allowedDecisions": [
            decision.value for decision in approval.allowed_decisions
        ],
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
        _merge_message(assistant_content, message)
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
    content: list[dict[str, object]] = []
    for message in messages[len(_through_latest_user(messages)) :]:
        _merge_message(content, message)
    return content


def _merge_message(content: list[dict[str, object]], message: Message) -> bool:
    """Fold one engine message into one assistant-ui assistant response."""
    changed = False
    if message.role is Role.ASSISTANT:
        if message.content:
            content.append({"type": "text", "text": message.content})
            changed = True
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            content.append(
                {
                    "type": "tool-call",
                    "toolCallId": call.call_id,
                    "toolName": call.name,
                    "args": arguments,
                    "argsText": call.arguments,
                }
            )
            changed = True
    elif message.role is Role.TOOL and message.tool_call_id:
        for part in reversed(content):
            if part.get("toolCallId") == message.tool_call_id:
                part["result"] = message.content
                changed = True
                break
    return changed


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
