"""Agent Runner capability, backed by the Claude Code CLI.

``ClaudeCodeAgentRunner`` runs `claude -p --output-format stream-json
--verbose`, and adds stream-JSON input and Claude's control protocol when the
turn may pause: a permission callback then stops and resumes the same run.
Which invocation is used is the caller's choice of method, not a choice of
runner. The records this package parses:

    {"type":"system","subtype":"init","session_id":"4aeecd85-…","tools":[…]}
    {"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_…",
                                               "name":"Glob","input":{…}}]}}
    {"type":"user","message":{"content":[{"type":"tool_result",
                                          "tool_use_id":"toolu_…","content":"…"}]}}
    {"type":"assistant","message":{"content":[{"type":"text","text":"…"}]}}
    {"type":"result","subtype":"success","is_error":false,"result":"…",
     "usage":{"input_tokens":4,"cache_creation_input_tokens":6478,
              "cache_read_input_tokens":41156,"output_tokens":149},
     "total_cost_usd":0.0897}

Two differences from the Codex adapter, both in this one's favour:

* **Tool calls arrive structured.** `tool_use` and `tool_result` blocks carry
  ids, so pairing a call with its result is reading a field rather than
  inferring from order.
* **Caching actually happens.** Claude Code reports `cache_read_input_tokens`
  around 41k against 4 raw input tokens -- roughly 86% of the prompt served from
  cache, where `codex exec` sits at 65% and pins there regardless of what we
  send. Worth knowing when reading the `TODO(caching)` note in the Codex
  adapter: that problem is `codex exec` rebuilding a process per turn, not
  something inherent to driving a CLI.

Like Codex, Claude Code brings its own tools and cannot be handed ours, so a
profile with grants is refused rather than quietly run without them. What it can
be told is *which* of its own tools to use, via `allowed_tools` -- the read-only
default is this adapter's equivalent of Codex's read-only sandbox. Workflow
runs additionally attach the runtime's bound terminal MCP tools. In an
interactive turn, other built-in tools fall through to the approval callback.

Stdlib only: a subprocess and a JSON parser.
"""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

from engine.adapters.agent_runner.claude_code.permissions import (
    CLAUDE_PERMISSION_TRANSLATOR,
    ClaudePermissionTranslator,
    allowed_tools_for,
)
from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, ToolCall
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import (
    AgentTurn,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalKind,
    ApprovalRequest,
    FinishReason,
    McpServerConfig,
    ResponseStyle,
    TokenUsage,
    TurnObserver,
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
)
from engine.ports.workspace_provider import WorkspaceProvider
from engine.runtime.protocol_diagnostics import (
    AgentProtocolDiagnostics,
    interaction_rejection_message,
)
from engine.runtime.streams import read_lines
from engine.runtime.transcript import flatten

#: Claude Code's own tools that only read. The default, because answering should
#: not change the tree on its own: anything outside this list reaches the
#: approval callback in an interactive turn, and is denied by the CLI in a turn
#: with nobody to ask.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

#: File operations a workflow implementation agent may use inside its isolated
#: worktree. Shell access is intentionally absent: unlike Codex, Claude Code
#: does not provide an OS-level workspace-only sandbox for Bash commands.
WORKSPACE_WRITE_TOOLS = (*READ_ONLY_TOOLS, "Edit", "Write")

#: Content blocks that are not worth recording. Thinking blocks are the model's
#: working, not the conversation's.
IGNORED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

CLAUDE_FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

#: Claude Code's own output-style names, by the Engine style that selects them.
#: The capitalization is part of the name: the CLI matches a style by its exact
#: title and silently keeps its default for anything else, so this table is the
#: only place a style name is spelled and an untranslatable style is never
#: guessed at.
OUTPUT_STYLES: dict[ResponseStyle, str] = {
    ResponseStyle.CONCISE: "Concise",
    ResponseStyle.EXPLANATORY: "Explanatory",
    ResponseStyle.LEARNING: "Learning",
}


class ClaudeUnavailableError(RuntimeError):
    """The `claude` binary is not on PATH."""


class ClaudeExecutionError(RuntimeError):
    """Claude Code ran and failed, timed out, or produced no answer."""


class ClaudeToolsUnsupportedError(NotImplementedError):
    """A profile granted tools that Claude Code cannot be offered.

    Raised rather than ignored, for the same reason as the Codex adapter: an
    agent quietly less capable than its profile promises is worse than one that
    refuses to start. Only the workflow terminal tools have an MCP bridge.
    """

    def __init__(self, tool_names: Sequence[str]) -> None:
        super().__init__(
            f"Claude Code runs its own tools and cannot be offered {list(tool_names)}; "
            "only the runtime-bound workflow terminal tools are available over MCP"
        )
        self.tool_names = tuple(tool_names)


def parse_events(stdout: str) -> tuple[dict[str, Any], ...]:
    """JSONL to dicts, skipping anything that is not a JSON object."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


def approval_request_from_control(message: dict[str, Any]) -> ApprovalRequest | None:
    """Normalize Claude Code's ``can_use_tool`` control request."""
    if message.get("type") != "control_request" or "request_id" not in message:
        return None
    request = message.get("request") or {}
    if not isinstance(request, dict) or request.get("subtype") != "can_use_tool":
        return None

    tool_name = str(request.get("tool_name", "tool"))
    tool_input = request.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"input": tool_input}
    questions: tuple[UserInputQuestion, ...] = ()
    requires_human = tool_name in {"AskUserQuestion", "ExitPlanMode"}
    if tool_name == "AskUserQuestion":
        kind = ApprovalKind.USER_INPUT
        questions = _questions_from_claude(tool_input)
    elif tool_name == "ExitPlanMode":
        kind = ApprovalKind.PLAN_APPROVAL
    elif tool_name == "Bash":
        kind = ApprovalKind.COMMAND_EXECUTION
    elif tool_name in CLAUDE_FILE_TOOLS:
        kind = ApprovalKind.FILE_CHANGE
    else:
        kind = ApprovalKind.TOOL_USE

    suggestions = request.get("permission_suggestions")
    allowed = [] if questions else [ApprovalDecision.ACCEPT]
    if not requires_human and isinstance(suggestions, list) and suggestions:
        allowed.append(ApprovalDecision.ACCEPT_FOR_SESSION)
    allowed.append(ApprovalDecision.CANCEL)

    reason = next(
        (
            str(request[field])
            for field in ("title", "decision_reason", "description")
            if request.get(field)
        ),
        None,
    )
    command = tool_input.get("command") if tool_name == "Bash" else None
    # `tool_use_id` is the id of the `tool_use` block this run already recorded
    # as a `ToolCall`, so the pause names the call in the transcript rather than
    # merely describing it. A control request without one is a request about
    # nothing we stored, and says so.
    tool_use_id = request.get("tool_use_id")
    return ApprovalRequest(
        approval_id=str(tool_use_id or message["request_id"]),
        kind=kind,
        reason=reason,
        command=str(command) if command is not None else None,
        tool_name=tool_name,
        tool_call_id=str(tool_use_id) if tool_use_id else None,
        arguments=json.dumps(tool_input, sort_keys=True),
        allowed_decisions=tuple(allowed),
        questions=questions,
        requires_human=requires_human,
    )


def _control_diagnostic_shape(message: dict[str, Any]) -> dict[str, Any]:
    """Describe a Claude control request without retaining its input values."""
    request = message.get("request")
    shaped: dict[str, Any] = {
        "interaction_type": str(message.get("type", "")),
        "request_id": str(message["request_id"])
        if "request_id" in message
        else None,
        "request_type": type(request).__name__,
    }
    if isinstance(request, dict):
        shaped.update(
            {
                "request_keys": sorted(str(key) for key in request),
                "subtype": str(request["subtype"])
                if "subtype" in request
                else None,
                "tool_name": str(request["tool_name"])
                if "tool_name" in request
                else None,
                "input_type": type(request.get("input")).__name__,
            }
        )
    return shaped


def control_response_for(
    message: dict[str, Any], decision: ApprovalDecision | UserInputResponse
) -> dict[str, Any]:
    """Build the Claude control-protocol response for one Engine decision."""
    request = message.get("request") or {}
    tool_input = request.get("input") or {}
    if isinstance(decision, UserInputResponse):
        questions = tool_input.get("questions")
        by_question = {
            answer.question_id: ", ".join(answer.answers)
            for answer in decision.answers
        }
        response = {
            "behavior": "allow",
            "updatedInput": {
                **tool_input,
                "questions": questions,
                "answers": by_question,
            },
        }
    elif decision is ApprovalDecision.CANCEL:
        response: dict[str, Any] = {
            "behavior": "deny",
            "message": "Cancelled by user",
            "interrupt": True,
        }
    else:
        response = {"behavior": "allow", "updatedInput": tool_input}
        if decision is ApprovalDecision.ACCEPT_FOR_SESSION:
            suggestions = request.get("permission_suggestions") or []
            response["updatedPermissions"] = [
                {**suggestion, "destination": "session"}
                for suggestion in suggestions
                if isinstance(suggestion, dict)
            ]
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": message["request_id"],
            "response": response,
        },
    }


def _questions_from_claude(tool_input: dict[str, Any]) -> tuple[UserInputQuestion, ...]:
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list):
        return ()
    questions: list[UserInputQuestion] = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question", "")).strip()
        if not question:
            continue
        options = raw.get("options")
        normalized_options = (
            tuple(
                UserInputOption(
                    label=str(option.get("label", "")),
                    description=str(option.get("description", "")),
                )
                for option in options
                if isinstance(option, dict)
                and str(option.get("label", "")).strip()
            )
            if isinstance(options, list)
            else ()
        )
        questions.append(
            UserInputQuestion(
                # Claude's answers object is keyed by the full question text.
                question_id=question,
                header=str(raw.get("header", f"Question {index + 1}")),
                question=question,
                options=normalized_options,
                multi_select=bool(raw.get("multiSelect", False)),
                allows_other=True,
            )
        )
    return tuple(questions)


async def _write_json(stream: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    stream.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await stream.drain()


async def _next_json_message(lines: AsyncIterator[bytes]) -> dict[str, Any]:
    async for line in lines:
        parsed = parse_events(line.decode(errors="replace"))
        if parsed:
            return parsed[0]
    raise ClaudeExecutionError("Claude Code closed its event stream unexpectedly")


async def _read_initialize_response(
    lines: AsyncIterator[bytes], request_id: str, events: list[dict[str, Any]]
) -> None:
    while True:
        message = await _next_json_message(lines)
        response = message.get("response") or {}
        if (
            message.get("type") == "control_response"
            and isinstance(response, dict)
            and response.get("request_id") == request_id
        ):
            if response.get("subtype") == "error":
                raise ClaudeExecutionError(
                    f"Claude control initialization failed: {response.get('error')}"
                )
            return
        events.append(message)


def session_id_of(events: Iterable[dict[str, Any]]) -> str | None:
    """Claude Code's own session id, if it announced one.

    Not used yet; it is what `claude --resume` would need.
    """
    for event in events:
        if event.get("session_id"):
            return str(event["session_id"])
    return None


def _block_text(content: Any) -> str:
    """A tool result's content, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part) or json.dumps(content, sort_keys=True)
    return "" if content is None else json.dumps(content, sort_keys=True)


def _usage_of(event: dict[str, Any]) -> TokenUsage:
    """Claude reports fresh, cache-written and cache-read input separately.

    They are summed into `prompt_tokens` so the number means the same thing it
    does for any other runner: everything the model was given.
    """
    reported = event.get("usage") or {}
    fresh = int(reported.get("input_tokens", 0))
    written = int(reported.get("cache_creation_input_tokens", 0))
    read = int(reported.get("cache_read_input_tokens", 0))
    cost = event.get("total_cost_usd")
    return TokenUsage(
        prompt_tokens=fresh + written + read,
        completion_tokens=int(reported.get("output_tokens", 0)),
        cached_prompt_tokens=read,
        cost_usd=float(cost) if cost is not None else None,
    )


def messages_from_event(event: dict[str, Any]) -> tuple[Message, ...]:
    """Conversation messages completed by one Claude Code event."""
    messages: list[Message] = []
    match event.get("type"):
        case "assistant":
            for block in (event.get("message") or {}).get("content") or ():
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind in IGNORED_BLOCK_TYPES:
                    continue
                if kind == "text" and str(block.get("text", "")).strip():
                    messages.append(Message.assistant(str(block["text"])))
                elif kind == "tool_use":
                    call = ToolCall(
                        call_id=str(block.get("id", "")),
                        name=str(block.get("name", "tool")),
                        arguments=json.dumps(block.get("input") or {}, sort_keys=True),
                    )
                    messages.append(Message.assistant(tool_calls=(call,)))
        case "user":
            for block in (event.get("message") or {}).get("content") or ():
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _block_text(block.get("content"))
                if block.get("is_error"):
                    text = f"error: {text}"
                messages.append(Message.tool_result(str(block.get("tool_use_id", "")), text))
    return tuple(messages)


def turn_from_events(events: Iterable[dict[str, Any]]) -> AgentTurn:
    """Assemble the answer, and everything the agent did to reach it.

    The last text block is the answer; every earlier one is narration, and every
    tool call and result is recorded in between. The `result` record's own text
    is used only as a fallback, so a turn that ends in a tool loop still reports
    something rather than raising.
    """
    entries: list[tuple[str, Any]] = []
    usage: TokenUsage | None = None
    failed = False
    reported_answer: str | None = None

    for event in events:
        match event.get("type"):
            case "assistant":
                for block in (event.get("message") or {}).get("content") or ():
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind in IGNORED_BLOCK_TYPES:
                        continue
                    if kind == "text" and str(block.get("text", "")).strip():
                        entries.append(("message", str(block["text"])))
                    elif kind == "tool_use":
                        entries.append(
                            (
                                "call",
                                ToolCall(
                                    call_id=str(block.get("id", "")),
                                    name=str(block.get("name", "tool")),
                                    arguments=json.dumps(block.get("input") or {}, sort_keys=True),
                                ),
                            )
                        )
            case "user":
                for block in (event.get("message") or {}).get("content") or ():
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    text = _block_text(block.get("content"))
                    if block.get("is_error"):
                        text = f"error: {text}"
                    entries.append(("result", (str(block.get("tool_use_id", "")), text)))
            case "result":
                usage = _usage_of(event)
                failed = bool(event.get("is_error"))
                if event.get("result"):
                    reported_answer = str(event["result"])

    spoken = [index for index, (kind, _) in enumerate(entries) if kind == "message"]
    answer_at = spoken[-1] if spoken else None

    steps: list[Message] = []
    for index, (kind, payload) in enumerate(entries):
        if index == answer_at:
            continue
        match kind:
            case "message":
                steps.append(Message.assistant(payload))
            case "call":
                steps.append(Message.assistant(tool_calls=(payload,)))
            case "result":
                call_id, text = payload
                steps.append(Message.tool_result(call_id, text))

    answer = entries[answer_at][1] if answer_at is not None else reported_answer
    if answer is None:
        raise ClaudeExecutionError("Claude Code produced no assistant message")

    return AgentTurn(
        message=Message.assistant(answer),
        finish_reason=FinishReason.ERROR if failed else FinishReason.STOP,
        usage=usage,
        steps=tuple(steps),
    )


class ClaudeCodeAgentRunner:
    """Runs an agent turn by shelling out to the Claude Code CLI.

    Implements `engine.ports.AgentRunner`, `StreamingAgentRunner`,
    `InteractiveAgentRunner`, `McpAgentRunner`, and `StreamingMcpAgentRunner`
    -- one runner rather than one per invocation, because whether a turn can
    pause for permission is a property of the turn being asked for and not of
    the agent answering it.

    `timeout_seconds=None` -- the default -- lets a turn take as long as it
    takes, for the reasons the Codex runner's docstring gives: a long run is
    usually a large task, and `cancel` is the way one ends early.
    """

    permission_translator = CLAUDE_PERMISSION_TRANSLATOR

    def __init__(
        self,
        binary_path: str = "claude",
        timeout_seconds: float | None = None,
        protocol_timeout_seconds: float = 60.0,
        allowed_tools: Sequence[str] = READ_ONLY_TOOLS,
        working_directory: str = ".",
        model: str = "",
        workspace_provider: WorkspaceProvider | None = None,
        attribution: bool = True,
        output_style: ResponseStyle | None = None,
    ) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds
        self._protocol_timeout_seconds = protocol_timeout_seconds
        self._allowed_tools = tuple(allowed_tools)
        self._working_directory = working_directory
        self._model = model
        self._attribution = attribution
        self._output_style = output_style
        self._workspace_provider = workspace_provider
        self._running: dict[AgentRunId, asyncio.subprocess.Process] = {}

    def command_line(
        self, profile: AgentProfile, mcp_server: McpServerConfig | None = None
    ) -> list[str]:
        """The argv this runner would use. Public so the wiring is inspectable
        without running anything."""
        argv = [self._binary_path, "-p", "--output-format", "stream-json", "--verbose"]
        # One `--settings` for every provider setting Engine configures: the
        # flag takes a whole document, so a second occurrence would replace the
        # first rather than add to it.
        settings: dict[str, Any] = {}
        if not self._attribution:
            settings["attribution"] = {"commit": "", "pr": "", "sessionUrl": False}
        if self._output_style is not None:
            settings["outputStyle"] = OUTPUT_STYLES[self._output_style]
        if settings:
            argv += ["--settings", json.dumps(settings, separators=(",", ":"))]
        if profile.instructions.strip():
            # A real system-prompt channel, unlike `codex exec` -- so the
            # instructions never enter the conversation text.
            argv += ["--append-system-prompt", profile.instructions.strip()]
        if mcp_server is not None:
            argv += [
                "--mcp-config",
                json.dumps(
                    {
                        "mcpServers": {
                            mcp_server.name: {
                                "command": mcp_server.command,
                                "args": list(mcp_server.args),
                            }
                        }
                    },
                    separators=(",", ":"),
                ),
            ]
        allowed_tools = self._allowed_tools
        if mcp_server is not None:
            allowed_tools = (
                *allowed_tools,
                f"mcp__{mcp_server.name}__clarify",
                f"mcp__{mcp_server.name}__complete_step",
                f"mcp__{mcp_server.name}__fail_step",
                # Reporting rather than acting: it says a sentence in the
                # conversation the run came from and touches nothing else, so
                # it belongs with the other three that no profile grants. The
                # broker serves it only when there is somewhere to report to,
                # and naming it here when there is not costs nothing -- a tool
                # the server does not list is a tool the model never sees.
                f"mcp__{mcp_server.name}__update_status",
                *(
                    f"mcp__{mcp_server.name}__{capability}"
                    for capability in profile.capabilities
                ),
            )
        if allowed_tools:
            # Variadic, and an empty list would swallow the next flag.
            argv += ["--allowedTools", *allowed_tools]
        model = profile.model or self._model
        if model:
            argv += ["--model", model]
        return argv

    def interactive_command_line(
        self, profile: AgentProfile, mcp_server: McpServerConfig | None = None
    ) -> list[str]:
        """The bidirectional stream-JSON invocation used for approval turns."""
        return [
            *self.command_line(profile, mcp_server),
            "--input-format",
            "stream-json",
            "--permission-prompt-tool",
            "stdio",
        ]

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self.run_turn_streamed(
            agent_run_id,
            profile,
            messages,
            lambda _message: None,
            tools=tools,
            workspace_id=workspace_id,
        )

    async def run_turn_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_message: TurnObserver,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
        mcp_server: McpServerConfig | None = None,
    ) -> AgentTurn:
        """Run Claude Code, forwarding each completed JSONL block immediately."""
        if tools:
            raise ClaudeToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None and self._workspace_provider is None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise ClaudeUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Claude Code CLI, "
                "or point the runner at the binary"
            )

        working_directory = self._working_directory
        if workspace_id is not None:
            assert self._workspace_provider is not None
            working_directory = await self._workspace_provider.root_path(workspace_id)

        process = await asyncio.create_subprocess_exec(
            *self.command_line(profile, mcp_server),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
        )
        self._running[agent_run_id] = process
        try:
            events, stderr = await asyncio.wait_for(
                self._read_stream(process, flatten(messages), on_message),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise ClaudeExecutionError(
                f"Claude Code did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        except asyncio.CancelledError:
            # The caller gave up -- do not leave the CLI running behind them.
            process.kill()
            await process.wait()
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            raise ClaudeExecutionError(
                f"claude exited {process.returncode}: {_tail(stderr)}"
            )
        return turn_from_events(events)

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval: ApprovalHandler,
        on_message: TurnObserver | None = None,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
        mcp_server: McpServerConfig | None = None,
    ) -> AgentTurn:
        """Run Claude Code with its Agent SDK-compatible control protocol."""
        if tools:
            raise ClaudeToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None and self._workspace_provider is None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise ClaudeUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Claude Code CLI, "
                "or point the runner at the binary"
            )

        working_directory = self._working_directory
        if workspace_id is not None:
            assert self._workspace_provider is not None
            working_directory = await self._workspace_provider.root_path(workspace_id)

        process = await asyncio.create_subprocess_exec(
            *self.interactive_command_line(profile, mcp_server),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
        )
        self._running[agent_run_id] = process
        try:
            events, stderr = await asyncio.wait_for(
                self._read_interactive_stream(
                    process,
                    agent_run_id,
                    working_directory,
                    flatten(messages),
                    on_approval,
                    on_message or (lambda _message: None),
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise ClaudeExecutionError(
                f"Claude Code did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            raise ClaudeExecutionError(
                f"claude interactive stream exited {process.returncode}: {_tail(stderr)}"
            )
        return turn_from_events(events)

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_approval: ApprovalHandler,
        on_message: TurnObserver | None = None,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run a control-protocol turn with approvals and workflow tools."""
        return await self.run_turn_interactive(
            agent_run_id,
            profile,
            messages,
            on_approval,
            on_message=on_message,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
        )

    async def _read_interactive_stream(
        self,
        process: asyncio.subprocess.Process,
        agent_run_id: AgentRunId,
        working_directory: str,
        prompt: str,
        on_approval: ApprovalHandler,
        on_message: TurnObserver,
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        """Drive Claude's stream-JSON input and permission control protocol."""
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(process.stderr.read())
        lines = read_lines(process.stdout).__aiter__()
        events: list[dict[str, Any]] = []
        observed: list[Message] = []
        diagnostics = AgentProtocolDiagnostics.for_run(
            "claude_code",
            agent_run_id,
            shutil.which(self._binary_path) or self._binary_path,
            working_directory,
        )
        diagnostics.record("session_started", adapter_file=__file__)
        try:
            initialize_id = "engine-initialize"
            await _write_json(
                process.stdin,
                {
                    "type": "control_request",
                    "request_id": initialize_id,
                    "request": {"subtype": "initialize", "hooks": None},
                },
            )
            try:
                await asyncio.wait_for(
                    _read_initialize_response(lines, initialize_id, events),
                    timeout=self._protocol_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise ClaudeExecutionError(
                    "Claude Code did not complete control initialization within "
                    f"{self._protocol_timeout_seconds:g}s"
                ) from error
            diagnostics.record("session_initialized", adapter_file=__file__)

            await _write_json(
                process.stdin,
                {
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                },
            )

            while True:
                message = await _next_json_message(lines)
                request = approval_request_from_control(message)
                if message.get("type") == "control_request":
                    diagnostics.record(
                        "interaction_received",
                        adapter_file=__file__,
                        **_control_diagnostic_shape(message),
                    )
                if request is not None:
                    diagnostics.record(
                        "interaction_normalized",
                        adapter_file=__file__,
                        request_id=str(message["request_id"]),
                        approval_kind=request.kind.value,
                        question_count=len(request.questions),
                    )
                    decision = await on_approval(request)
                    if (
                        isinstance(decision, ApprovalDecision)
                        and decision not in request.allowed_decisions
                    ):
                        raise ClaudeExecutionError(
                            f"approval decision {decision.value!r} is not allowed for "
                            f"{request.approval_id}"
                        )
                    response = control_response_for(message, decision)
                    diagnostics.record(
                        "interaction_response_sent",
                        adapter_file=__file__,
                        request_id=str(message["request_id"]),
                        decision=(
                            "user_input"
                            if isinstance(decision, UserInputResponse)
                            else decision.value
                        ),
                    )
                    await _write_json(process.stdin, response)
                    continue

                if message.get("type") == "control_request":
                    request_id = message.get("request_id")
                    subtype = (message.get("request") or {}).get("subtype")
                    rejection_message = interaction_rejection_message(
                        str(subtype), "unsupported_subtype"
                    )
                    diagnostics.record(
                        "interaction_rejected",
                        adapter_file=__file__,
                        **_control_diagnostic_shape(message),
                        rejection_reason="unsupported_subtype",
                    )
                    await _write_json(
                        process.stdin,
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "error",
                                "request_id": request_id,
                                "error": rejection_message,
                            },
                        },
                    )
                    diagnostics.record(
                        "interaction_response_sent",
                        adapter_file=__file__,
                        request_id=str(request_id),
                        response_error_code="unsupported_control_request",
                    )
                    continue
                if message.get("type") in {"control_response", "control_cancel_request"}:
                    continue

                events.append(message)
                for completed in messages_from_event(message):
                    observed.append(completed)
                    on_message(completed)
                if message.get("type") == "result":
                    break

            process.stdin.close()
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

        if process.returncode == 0:
            transcript = turn_from_events(events).transcript
            if transcript[: len(observed)] == tuple(observed):
                for completed in transcript[len(observed) :]:
                    on_message(completed)
        return tuple(events), stderr

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run a turn with the runtime's bound terminal tools attached."""
        return await self.run_turn_with_mcp_streamed(
            agent_run_id,
            profile,
            messages,
            mcp_server,
            lambda _message: None,
            workspace_id,
        )

    async def run_turn_with_mcp_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_message: TurnObserver,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run with terminal tools while reporting completed transcript messages."""
        return await self.run_turn_streamed(
            agent_run_id,
            profile,
            messages,
            on_message,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
        )

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
        on_message: TurnObserver,
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        """Feed stdin and drain both output pipes without buffering stdout."""
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        stderr_task = asyncio.create_task(process.stderr.read())
        events: list[dict[str, Any]] = []
        observed: list[Message] = []
        try:
            async for line in read_lines(process.stdout):
                for event in parse_events(line.decode(errors="replace")):
                    events.append(event)
                    for message in messages_from_event(event):
                        observed.append(message)
                        on_message(message)
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

        if process.returncode == 0:
            transcript = turn_from_events(events).transcript
            if transcript[: len(observed)] == tuple(observed):
                for message in transcript[len(observed) :]:
                    on_message(message)
        return tuple(events), stderr

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        """Terminate the run if it is still going. Safe to call otherwise."""
        process = self._running.get(agent_run_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()


def _tail(text: str, lines: int = 5) -> str:
    """The last few lines of stderr -- enough to diagnose, short enough to read."""
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:]) if kept else "(no stderr)"


__all__ = [
    "CLAUDE_PERMISSION_TRANSLATOR",
    "OUTPUT_STYLES",
    "READ_ONLY_TOOLS",
    "WORKSPACE_WRITE_TOOLS",
    "ClaudeCodeAgentRunner",
    "ClaudeExecutionError",
    "ClaudePermissionTranslator",
    "ClaudeToolsUnsupportedError",
    "ClaudeUnavailableError",
    "allowed_tools_for",
    "approval_request_from_control",
    "control_response_for",
    "messages_from_event",
    "parse_events",
    "session_id_of",
    "turn_from_events",
]
