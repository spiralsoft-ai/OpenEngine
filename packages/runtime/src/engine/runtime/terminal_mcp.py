"""Run-bound workflow tools over MCP.

The provider CLI launches this module as a stdio MCP server. It deliberately
contains no workflow identifiers: an opaque credential connects it to the
in-process broker that already owns the run, agent run, and step context.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import secrets
import shlex
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from engine.domain import (
    AgentRunId,
    ApprovalDecision,
    ApprovalKind,
    RunFailed,
    RunId,
    StepCompleted,
    StepSpec,
)
from engine.domain.ids import WorkspaceId
from engine.ports import ApprovalHandler, ApprovalRequest, McpServerConfig, SourceControl
from engine.runtime.step_results import (
    InvalidStepResultError,
    run_failed_from_arguments,
    step_completed_from_arguments,
)

TerminalEvent = StepCompleted | RunFailed
TerminalDelivery = Callable[[TerminalEvent], Awaitable[None]]
McpRequestId = str | int

#: Given a tool's provider-facing name and the arguments it was called with,
#: the id the transcript records that call under -- or ``None`` while the
#: provider has not reported the call yet. The MCP request id cannot stand in
#: for it: this server is reached over its own transport, and the number it
#: numbers a request with is not anything the conversation contains.
ToolCallLookup = Callable[[str, str], str | None]

#: Given a line of progress the agent wants a person to see, put it in front of
#: them. Bound by whoever knows where the run is being watched.
StatusReporter = Callable[[str], Awaitable[None]]

_SERVER_NAME = "workflow"
_PROTOCOL_VERSION = "2025-06-18"

#: Which `SourceControl` method each repository tool is a front for. A grant
#: is only served when the composed source control actually has its method, so
#: this is also the list of what "can this be served" is asked about -- one
#: table rather than a condition written out per tool.
REPOSITORY_TOOL_METHODS: dict[str, str] = {
    "git_subcommand": "run_git",
    "open_pull_request": "request_review",
    "add_comment": "add_comment",
    "view_change_request": "view_change_request",
    "list_work_items": "list_work_items",
    "view_work_item": "view_work_item",
    "list_pipeline_status": "list_pipeline_status",
    "get_job_logs": "get_job_logs",
    "retry_pipeline": "retry_pipeline",
}

#: The repository tools, in the order a server lists them.
REPOSITORY_TOOL_NAMES: tuple[str, ...] = tuple(REPOSITORY_TOOL_METHODS)

#: What `open_pull_request` proposes against when the agent names no base.
DEFAULT_BASE_REF = "main"


class TerminalResultAlreadySubmittedError(RuntimeError):
    """An agent run already owns an accepted terminal result."""


@dataclass(slots=True)
class TerminalResultRegistry:
    """Process-local single-submission guard shared by terminal sessions."""

    _accepted: dict[AgentRunId, TerminalEvent] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def accept(
        self,
        agent_run_id: AgentRunId,
        event: TerminalEvent,
        deliver: TerminalDelivery | None,
    ) -> None:
        async with self._lock:
            previous = self._accepted.get(agent_run_id)
            if previous is not None:
                raise TerminalResultAlreadySubmittedError(
                    "a terminal result was already accepted for this agent run"
                )
            if deliver is not None:
                await deliver(event)
            self._accepted[agent_run_id] = event


class TerminalMcpBroker:
    """Bind one local MCP bridge to one workflow execution context.

    A broker with no step serves the repository tools and nothing else. That is
    the naming turn: it reads the repository to say what a run is about, and
    there is no step for `complete_step` to complete -- so offering the terminal
    tools would be offering a turn that cannot end this way a way to end it.
    """

    def __init__(
        self,
        *,
        run_id: RunId,
        agent_run_id: AgentRunId,
        step: StepSpec | None,
        registry: TerminalResultRegistry,
        deliver: TerminalDelivery | None = None,
    ) -> None:
        self._run_id = run_id
        self._agent_run_id = agent_run_id
        self._step = step
        self._registry = registry
        self._deliver = deliver
        # Hex rather than URL-safe base64, because this credential is handed to
        # the provider as an argv element: `token_urlsafe` can begin with `-`,
        # and roughly one session in sixty-four then had its server exit on
        # `--token: expected one argument` before answering `initialize`. Same
        # 256 bits, out of an alphabet nothing reads as an option.
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._result: asyncio.Future[TerminalEvent] | None = None
        self._source_control: SourceControl | None = None
        self._repository_tools: tuple[str, ...] = ()
        self._workspace_id: WorkspaceId | None = None
        self._git_approval: ApprovalHandler | None = None
        self._tool_call_ids: ToolCallLookup | None = None
        self._comments_added = 0
        self._status_reporter: StatusReporter | None = None

    def enable_status_updates(self, report: StatusReporter) -> None:
        """Serve `update_status`, delivering what it is told to `report`.

        Only bound when the run has somewhere to be reported to. A step whose
        run was started from the web is not offered the tool at all, rather
        than offered one whose updates go nowhere.
        """
        self._status_reporter = report

    def enable_repository_tools(
        self,
        source_control: SourceControl,
        names: Sequence[str],
        workspace_id: WorkspaceId | None = None,
        git_approval: ApprovalHandler | None = None,
        tool_call_ids: ToolCallLookup | None = None,
    ) -> None:
        """Expose named repository operations through this run-bound server.

        `names` is what the step's profile was granted and the composition can
        honour, decided by the dispatcher; the broker only serves it. The
        workspace is the one the step is running in, and it is bound here
        rather than passed per call so a model cannot name a different one.

        `tool_call_ids` is how a request raised here names the call it is about.
        The dispatcher owns that answer because it is the side watching the
        provider's stream; without it a request can still be raised, and is
        shown at the end of the turn instead of beside the call.
        """

        self._source_control = source_control
        self._repository_tools = tuple(
            name for name in REPOSITORY_TOOL_NAMES if name in names
        )
        self._workspace_id = workspace_id
        self._git_approval = git_approval
        self._tool_call_ids = tool_call_ids

    async def __aenter__(self) -> TerminalMcpBroker:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", 0
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        connections = tuple(self._connections)
        for connection in connections:
            connection.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        if self._result is not None and not self._result.done():
            self._result.cancel()

    @property
    def config(self) -> McpServerConfig:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("terminal MCP broker has not been started")
        port = self._server.sockets[0].getsockname()[1]
        arguments = (
            "-m",
            "engine.runtime.terminal_mcp_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            self._token,
        )
        for name in self._repository_tools:
            arguments = (*arguments, "--repository-tool", name)
        if self._status_reporter is not None:
            arguments = (*arguments, "--status-updates")
        if self._step is None:
            arguments = (*arguments, "--repository-tools-only")
        return McpServerConfig(
            name=_SERVER_NAME,
            command=sys.executable,
            args=arguments,
        )

    async def result(self) -> TerminalEvent:
        if self._result is None:
            raise RuntimeError("terminal MCP broker has not been started")
        return await asyncio.shield(self._result)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = asyncio.current_task()
        assert connection is not None
        self._connections.add(connection)
        try:
            try:
                raw = await reader.readline()
                request = json.loads(raw)
                response = await self._submit(request)
            except Exception as error:
                response = {"ok": False, "error": f"invalid terminal request: {error}"}
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            with suppress(ConnectionError):
                await writer.drain()
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
        finally:
            self._connections.discard(connection)

    async def _submit(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "terminal session is not authorized"}
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, (str, int))
            or isinstance(request_id, bool)
        ):
            return {"ok": False, "error": "MCP request id must be a string or number"}
        name = request.get("name")
        arguments = request.get("arguments")
        try:
            if isinstance(name, str) and name in REPOSITORY_TOOL_NAMES:
                if name not in self._repository_tools:
                    return {"ok": False, "error": f"{name} is not enabled for this step"}
                return await self._repository_call(name, arguments, request_id)
            if name == "update_status":
                if self._status_reporter is None:
                    return {
                        "ok": False,
                        "error": "update_status is not enabled for this step",
                    }
                try:
                    status = _status_argument(arguments)
                    await self._status_reporter(status)
                except Exception as error:
                    # Reporting is not the work. A provider that is down is
                    # something the step is told about and carries on from.
                    return {"ok": False, "error": f"could not post the status: {error}"}
                return {"ok": True, "acknowledgement": "status posted"}
            if self._step is None:
                # Listed by nobody and served by nobody: a session with no step
                # says so rather than failing later on a step it does not have.
                return {
                    "ok": False,
                    "error": f"{name} is not available in this session",
                }
            if name == "clarify":
                if not isinstance(arguments, dict) or arguments:
                    return {
                        "ok": False,
                        "error": "clarify does not accept arguments",
                    }
                # Reported here rather than with the other two terminal tools,
                # because this one is not terminal: no event leaves the broker
                # for the executor to announce, so nothing else would see it.
                if self._status_reporter is not None:
                    try:
                        await self._status_reporter(
                            "answered a question without changing the work order"
                        )
                    except Exception:
                        pass
                return {"ok": True, "acknowledgement": "clarified"}
            if name == "complete_step":
                if "add_comment" in self._repository_tools and not self._comments_added:
                    return {
                        "ok": False,
                        "error": "add at least one pull-request comment before completing review",
                    }
                event: TerminalEvent = step_completed_from_arguments(
                    run_id=self._run_id,
                    step=self._step,
                    agent_run_id=self._agent_run_id,
                    arguments=arguments,
                    mcp_request_id=request_id,
                )
            elif name == "fail_step":
                event = run_failed_from_arguments(
                    run_id=self._run_id,
                    agent_run_id=self._agent_run_id,
                    arguments=arguments,
                    mcp_request_id=request_id,
                )
            else:
                return {"ok": False, "error": f"unknown terminal tool: {name}"}
            await self._registry.accept(
                self._agent_run_id, event, self._deliver
            )
        except (
            InvalidStepResultError,
            TerminalResultAlreadySubmittedError,
            ValueError,
        ) as error:
            return {"ok": False, "error": str(error)}
        assert self._result is not None
        if not self._result.done():
            self._result.set_result(event)
        return {"ok": True, "acknowledgement": "accepted"}

    async def _repository_call(
        self, name: str, arguments: object, request_id: McpRequestId
    ) -> dict[str, object]:
        """Run one repository tool against the composed source control.

        Nothing here reaches a terminal result, so a failure is answered rather
        than raised: a rejected push or a `gh` that is not logged in is
        something the step can read and act on, not a reason to end it.
        """

        assert self._source_control is not None
        if name == "add_comment":
            pr_url, comment, file, line = _comment_arguments(arguments)
            try:
                await self._source_control.add_comment(pr_url, comment, file, line)
            except Exception as error:
                return {"ok": False, "error": f"could not add comment: {error}"}
            self._comments_added += 1
            return {"ok": True, "acknowledgement": "comment added"}

        if self._workspace_id is None:
            return {"ok": False, "error": f"{name} needs a workspace and this step has none"}

        if name == "git_subcommand":
            git_arguments = _git_arguments(arguments)
            approved = await self._approve_git(git_arguments, request_id)
            if approved is not None:
                return approved
            try:
                result = await self._source_control.run_git(
                    self._workspace_id, git_arguments
                )
            except Exception as error:
                return {"ok": False, "error": f"could not run git: {error}"}
            reported = "\n".join(part for part in (result.stdout, result.stderr) if part)
            if not result.ok:
                return {
                    "ok": False,
                    "error": (
                        f"git exited {result.exit_code}: "
                        f"{reported or 'no output'}"
                    ),
                }
            # An empty answer is the normal one for half of git, and a tool
            # result with no text in it reads to a model as a tool that did
            # nothing. Say which command it was instead.
            return {
                "ok": True,
                "acknowledgement": "git ran",
                "output": reported or f"git {git_arguments[0]} exited 0 with no output",
            }

        if name == "view_change_request":
            try:
                result = await self._source_control.view_change_request(
                    self._workspace_id, _number_arguments(name, arguments, "number")
                )
            except Exception as error:
                return {"ok": False, "error": f"could not view change request: {error}"}
            return _repository_result(result)
        if name == "list_work_items":
            try:
                state, labels, limit = _list_work_items_arguments(arguments)
                result = await self._source_control.list_work_items(
                    self._workspace_id, state, labels, limit
                )
            except Exception as error:
                return {"ok": False, "error": f"could not list work items: {error}"}
            return _repository_result(result)
        if name == "view_work_item":
            try:
                result = await self._source_control.view_work_item(
                    self._workspace_id, _number_arguments(name, arguments, "number")
                )
            except Exception as error:
                return {"ok": False, "error": f"could not view work item: {error}"}
            return _repository_result(result)
        if name == "list_pipeline_status":
            try:
                ref, number = _pipeline_status_arguments(arguments)
                result = await self._source_control.list_pipeline_status(
                    self._workspace_id, ref=ref, change_request_number=number
                )
            except Exception as error:
                return {"ok": False, "error": f"could not list pipeline status: {error}"}
            return _repository_result(result)
        if name in {"get_job_logs", "retry_pipeline"}:
            try:
                pipeline_id, job_id = _pipeline_arguments(name, arguments)
                method = getattr(self._source_control, name)
                result = await method(self._workspace_id, pipeline_id, job_id)
            except Exception as error:
                action = "get job logs" if name == "get_job_logs" else "retry pipeline"
                return {"ok": False, "error": f"could not {action}: {error}"}
            return _repository_result(result)

        branch, base_ref, title, body = _review_arguments(arguments)
        try:
            url = await self._source_control.request_review(
                self._workspace_id, branch, base_ref, title, body
            )
        except Exception as error:
            return {"ok": False, "error": f"could not open the pull request: {error}"}
        return {"ok": True, "acknowledgement": "pull request opened", "output": url}

    async def _approve_git(
        self, arguments: tuple[str, ...], request_id: McpRequestId
    ) -> dict[str, object] | None:
        """Put arbitrary git through Engine's approval broker.

        Provider preapproval only controls whether the MCP request reaches this
        server. It cannot be the security boundary: git may invoke aliases,
        helpers and hooks, and the server runs outside the provider sandbox.
        The approval here is therefore required even when a provider was told
        that the profile holds `git_subcommand`.
        """

        if self._git_approval is None:
            return {
                "ok": False,
                "error": "git_subcommand requires approval handling for this step",
            }
        tool_name = f"mcp__{_SERVER_NAME}__git_subcommand"
        tool_arguments = json.dumps({"arguments": arguments}, sort_keys=True)
        request = ApprovalRequest(
            approval_id=f"terminal:{self._agent_run_id}:{request_id}",
            kind=ApprovalKind.TOOL_USE,
            reason="Run git in the step's bound workspace.",
            command=shlex.join(("git", *arguments)),
            tool_name=tool_name,
            # The provider's id for this call, not this server's id for the
            # request carrying it. Naming the request would put the pause
            # beside nothing, because no call in the transcript is called that.
            tool_call_id=(
                self._tool_call_ids(tool_name, tool_arguments)
                if self._tool_call_ids is not None
                else None
            ),
            arguments=tool_arguments,
            allowed_decisions=(
                ApprovalDecision.ACCEPT,
                ApprovalDecision.CANCEL,
            ),
        )
        try:
            decision = await self._git_approval(request)
        except Exception as error:
            return {"ok": False, "error": f"could not approve git: {error}"}
        if decision is not ApprovalDecision.ACCEPT:
            return {"ok": False, "error": "git_subcommand was not approved"}
        return None


def terminal_tool_names(
    repository_tools: Sequence[str] = (),
    *,
    terminal_tools: bool = True,
    status_updates: bool = False,
) -> tuple[str, ...]:
    """The tools a step's server serves, in the order it lists them.

    Read off the listing rather than restated beside it, so a tool added to
    `_tools` cannot end up served without the step being told it holds one.
    """
    return tuple(
        str(tool["name"])
        for tool in _tools(
            repository_tools,
            terminal_tools=terminal_tools,
            status_updates=status_updates,
        )
    )


def _tools(
    repository_tools: Sequence[str] = (),
    *,
    terminal_tools: bool = True,
    status_updates: bool = False,
) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = list(_TERMINAL_TOOLS) if terminal_tools else []
    if status_updates:
        tools.append(_STATUS_TOOL)
    tools.extend(
        _REPOSITORY_TOOLS[name]
        for name in REPOSITORY_TOOL_NAMES
        if name in repository_tools
    )
    return tools


#: Served only when the run has a conversation to report into.
_STATUS_TOOL: dict[str, object] = {
    "name": "update_status",
    "description": (
        "Tell the person who asked for this work order how it is going. The "
        "status is posted in the conversation the request came from, so write "
        "one plain sentence for a reader who cannot see the workspace. Use it "
        "when something changes that they would want to know about; it does "
        "not end the step."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"status": {"type": "string", "minLength": 1}},
        "required": ["status"],
        "additionalProperties": False,
    },
}


#: The tools every step's server serves, whatever it was granted.
_TERMINAL_TOOLS: tuple[dict[str, object], ...] = (
    {
        "name": "complete_step",
        "description": "Complete the bound workflow step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["success"]},
                "summary": {"type": "string"},
                "outputs": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["outcome", "summary", "outputs"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fail_step",
        "description": "Fail the bound workflow run when the step cannot continue.",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "minLength": 1}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clarify",
        "description": (
            "Finish answering a human question without changing workflow "
            "run state. Call this after the answer when no implementation "
            "change was requested or made."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)


#: The repository tools' declarations, by name.
#:
#: `git_subcommand` is a passthrough rather than one entry per operation, and
#: that is the point of it: git is the interface an agent already knows, and a
#: menu of `create_branch`/`commit`/`push` would keep meeting work that needs
#: the twentieth subcommand nobody put on the menu -- a rebase, a cherry-pick,
#: a `log -S` to find where something went. What is bounded is the checkout it
#: runs in, which the broker holds and the model cannot name.
_REPOSITORY_TOOLS: dict[str, dict[str, object]] = {
    "git_subcommand": {
        "name": "git_subcommand",
        "description": (
            "Run git in this step's workspace. `arguments` is everything that "
            "would follow `git`, one element per argument: "
            '["commit", "-m", "feat: add the thing"]. Any subcommand is '
            "available. No shell is involved, so quoting, globbing, pipes and "
            "redirection do not apply -- a multi-line commit message is simply "
            "one element. Every call requires Engine approval because git can "
            "invoke external helpers. Returns git's output; a non-zero exit is "
            "reported as an error with whatever git printed. Global options "
            "that select config or executables are refused. Pushes must name "
            "an explicit destination branch; implicit, HEAD, wildcard, --all, "
            "--branches and --mirror pushes are refused, as is any destination "
            "under Engine's internal engine/ prefix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                }
            },
            "required": ["arguments"],
            "additionalProperties": False,
        },
    },
    "open_pull_request": {
        "name": "open_pull_request",
        "description": (
            "Open a pull request for a branch already pushed to the remote, "
            "and return its URL. Push the branch with git_subcommand first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "minLength": 1},
                "base_ref": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
            },
            "required": ["branch", "title", "body"],
            "additionalProperties": False,
        },
    },
    "add_comment": {
        "name": "add_comment",
        "description": (
            "Add a comment to a pull request. Provide file and line together "
            "for an inline comment; omit both for a general comment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string", "minLength": 1},
                "comment": {"type": "string", "minLength": 1},
                "file": {"type": "string", "minLength": 1},
                "line": {"type": "integer", "minimum": 1},
            },
            "required": ["pr_url", "comment"],
            "dependentRequired": {"file": ["line"], "line": ["file"]},
            "additionalProperties": False,
        },
    },
    "view_change_request": {
        "name": "view_change_request",
        "description": "View a pull request or merge request in this workspace repository.",
        "inputSchema": {"type": "object", "properties": {"number": {"type": "integer", "minimum": 1}}, "required": ["number"], "additionalProperties": False},
    },
    "list_work_items": {
        "name": "list_work_items",
        "description": "List issues in this workspace repository; pull requests are excluded.",
        "inputSchema": {"type": "object", "properties": {"state": {"enum": ["open", "closed", "all"]}, "labels": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False},
    },
    "view_work_item": {
        "name": "view_work_item",
        "description": "View an issue and its comments in this workspace repository.",
        "inputSchema": {"type": "object", "properties": {"number": {"type": "integer", "minimum": 1}}, "required": ["number"], "additionalProperties": False},
    },
    "list_pipeline_status": {
        "name": "list_pipeline_status",
        "description": "List checks and CI pipelines for a ref or change request in this workspace repository.",
        "inputSchema": {"type": "object", "properties": {"ref": {"type": "string", "minLength": 1}, "change_request_number": {"type": "integer", "minimum": 1}}, "oneOf": [{"required": ["ref"]}, {"required": ["change_request_number"]}], "additionalProperties": False},
    },
    "get_job_logs": {
        "name": "get_job_logs",
        "description": "Get a bounded CI job-log excerpt for a pipeline in this workspace repository.",
        "inputSchema": {"type": "object", "properties": {"pipeline_id": {"type": "integer", "minimum": 1}, "job_id": {"type": "integer", "minimum": 1}}, "required": ["pipeline_id"], "additionalProperties": False},
    },
    "retry_pipeline": {
        "name": "retry_pipeline",
        "description": "Retry a CI pipeline, or one job and its dependents, in this workspace repository.",
        "inputSchema": {"type": "object", "properties": {"pipeline_id": {"type": "integer", "minimum": 1}, "job_id": {"type": "integer", "minimum": 1}}, "required": ["pipeline_id"], "additionalProperties": False},
    },
}


def _repository_result(result: object) -> dict[str, object]:
    if dataclasses.is_dataclass(result):
        value = dataclasses.asdict(result)
    elif isinstance(result, tuple) and all(dataclasses.is_dataclass(item) for item in result):
        value = [dataclasses.asdict(item) for item in result]
    else:
        value = result
    return {"ok": True, "acknowledgement": "repository data retrieved", "output": json.dumps(value, sort_keys=True)}


def _number_arguments(name: str, arguments: object, field: str) -> int:
    if not isinstance(arguments, dict) or set(arguments) - {field}:
        raise ValueError(f"unexpected {name} arguments")
    if field not in arguments:
        raise ValueError(f"{field} is required")
    value = arguments.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _list_work_items_arguments(arguments: object) -> tuple[str, tuple[str, ...], int]:
    if not isinstance(arguments, dict) or set(arguments) - {"state", "labels", "limit"}:
        raise ValueError("unexpected list_work_items arguments")
    state = arguments.get("state", "open")
    labels = arguments.get("labels", [])
    limit = arguments.get("limit", 30)
    if state not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    if not isinstance(labels, list) or not all(isinstance(label, str) and label for label in labels):
        raise ValueError("labels must be an array of non-empty strings")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return state, tuple(labels), limit


def _pipeline_status_arguments(arguments: object) -> tuple[str | None, int | None]:
    if not isinstance(arguments, dict) or set(arguments) - {"ref", "change_request_number"}:
        raise ValueError("unexpected list_pipeline_status arguments")
    ref, number = arguments.get("ref"), arguments.get("change_request_number")
    if (ref is None) == (number is None):
        raise ValueError("provide exactly one of ref or change_request_number")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise ValueError("ref must be a non-empty string")
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
        raise ValueError("change_request_number must be a positive integer")
    return ref, number


def _pipeline_arguments(name: str, arguments: object) -> tuple[int, int | None]:
    if not isinstance(arguments, dict) or set(arguments) - {"pipeline_id", "job_id"}:
        raise ValueError(f"unexpected {name} arguments")
    pipeline_id, job_id = arguments.get("pipeline_id"), arguments.get("job_id")
    if not isinstance(pipeline_id, int) or isinstance(pipeline_id, bool) or pipeline_id < 1:
        raise ValueError("pipeline_id must be a positive integer")
    if job_id is not None and (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1):
        raise ValueError("job_id must be a positive integer")
    return pipeline_id, job_id


def _status_argument(arguments: object) -> str:
    if not isinstance(arguments, dict) or set(arguments) - {"status"}:
        raise ValueError("unexpected update_status arguments")
    status = arguments.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    return status.strip()


def _git_arguments(arguments: object) -> tuple[str, ...]:
    if not isinstance(arguments, dict):
        raise ValueError("git_subcommand arguments must be an object")
    unexpected = set(arguments) - {"arguments"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected git_subcommand arguments: {names}")
    given = arguments.get("arguments")
    if not isinstance(given, list) or not given:
        raise ValueError("arguments must be a non-empty array of strings")
    if not all(isinstance(argument, str) for argument in given):
        raise ValueError("every element of arguments must be a string")
    return tuple(given)


def _review_arguments(arguments: object) -> tuple[str, str, str, str]:
    if not isinstance(arguments, dict):
        raise ValueError("open_pull_request arguments must be an object")
    unexpected = set(arguments) - {"branch", "base_ref", "title", "body"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected open_pull_request arguments: {names}")
    branch = arguments.get("branch")
    base_ref = arguments.get("base_ref", DEFAULT_BASE_REF)
    title = arguments.get("title")
    body = arguments.get("body", "")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch must be a non-empty string")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError("base_ref must be a non-empty string")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    return branch, base_ref, title, body


def _comment_arguments(
    arguments: object,
) -> tuple[str, str, str | None, int | None]:
    if not isinstance(arguments, dict):
        raise ValueError("add_comment arguments must be an object")
    unexpected = set(arguments) - {"pr_url", "comment", "file", "line"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected add_comment arguments: {names}")
    pr_url = arguments.get("pr_url")
    comment = arguments.get("comment")
    file = arguments.get("file")
    line = arguments.get("line")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise ValueError("pr_url must be a non-empty string")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("comment must be a non-empty string")
    if file is not None and (not isinstance(file, str) or not file.strip()):
        raise ValueError("file must be a non-empty string")
    if line is not None and (
        not isinstance(line, int) or isinstance(line, bool) or line < 1
    ):
        raise ValueError("line must be a positive integer")
    if (file is None) != (line is None):
        raise ValueError("file and line must be provided together")
    return pr_url, comment, file, line


async def _forward_call(
    host: str,
    port: int,
    token: str,
    request_id: McpRequestId,
    name: object,
    arguments: object,
) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()
    return response


async def _serve_stdio(
    host: str,
    port: int,
    token: str,
    *,
    repository_tools: Sequence[str] = (),
    terminal_tools: bool = True,
    status_updates: bool = False,
) -> None:
    """Serve newline-delimited MCP JSON-RPC without writing logs to stdout."""
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            request: Any = json.loads(line)
            response = await _mcp_response(
                host,
                port,
                token,
                request,
                repository_tools=repository_tools,
                terminal_tools=terminal_tools,
                status_updates=status_updates,
            )
            if response is None:
                continue
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {error}"},
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


async def _mcp_response(
    host: str,
    port: int,
    token: str,
    request: object,
    *,
    repository_tools: Sequence[str] = (),
    terminal_tools: bool = True,
    status_updates: bool = False,
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        # JSON-RPC notifications never receive responses.
        return None
    if method == "initialize":
        requested = request.get("params")
        protocol = (
            requested.get("protocolVersion", _PROTOCOL_VERSION)
            if isinstance(requested, dict)
            else _PROTOCOL_VERSION
        )
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engine-workflow-terminal", "version": "1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(
            request_id,
            {
                "tools": _tools(
                    repository_tools,
                    terminal_tools=terminal_tools,
                    status_updates=status_updates,
                )
            },
        )
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _rpc_error(request_id, -32600, "Tool calls require a request id")
    params = request.get("params")
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid tool parameters")
    forwarded = await _forward_call(
        host,
        port,
        token,
        request_id,
        params.get("name"),
        params.get("arguments", {}),
    )
    if forwarded.get("ok") is True:
        # Terminal tools acknowledge and nothing more; a repository tool has an
        # answer the step needs -- git's output, a pull-request URL -- and
        # returning a bare "accepted" for those would make the model guess at
        # what its own command printed.
        output = forwarded.get("output")
        if isinstance(output, str) and output:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": output}],
                    "structuredContent": {"accepted": True, "output": output},
                },
            )
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "accepted"}],
                "structuredContent": {"accepted": True},
            },
        )
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": str(forwarded.get("error"))}],
            "isError": True,
        },
    )


def _rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--repository-tool",
        action="append",
        default=[],
        choices=REPOSITORY_TOOL_NAMES,
        dest="repository_tools",
    )
    parser.add_argument(
        "--repository-tools-only",
        action="store_true",
        help="serve the granted repository tools without the terminal tools",
    )
    parser.add_argument(
        "--status-updates",
        action="store_true",
        help="serve update_status, for a run with a conversation to report to",
    )
    args = parser.parse_args()
    asyncio.run(
        _serve_stdio(
            args.host,
            args.port,
            args.token,
            repository_tools=tuple(args.repository_tools),
            terminal_tools=not args.repository_tools_only,
            status_updates=args.status_updates,
        )
    )


__all__ = [
    "DEFAULT_BASE_REF",
    "REPOSITORY_TOOL_METHODS",
    "REPOSITORY_TOOL_NAMES",
    "StatusReporter",
    "TerminalEvent",
    "TerminalMcpBroker",
    "TerminalResultAlreadySubmittedError",
    "TerminalResultRegistry",
    "ToolCallLookup",
    "terminal_tool_names",
]
