"""The Claude Code adapter, tested without spawning Claude.

Same shape as `test_codex_adapter.py`, and the transcript below is likewise
captured from a real run rather than invented -- a fixture of the wire format
you assumed only tests your assumption.
"""

import asyncio
import json
import textwrap

import pytest

from engine.adapters.agent_runner.claude_code import (
    CLAUDE_PERMISSION_TRANSLATOR,
    OUTPUT_STYLES,
    ClaudeCodeAgentRunner,
    ClaudeExecutionError,
    ClaudeToolsUnsupportedError,
    READ_ONLY_TOOLS,
    WORKSPACE_WRITE_TOOLS,
    allowed_tools_for,
    approval_request_from_control,
    control_response_for,
    parse_events,
    session_id_of,
    turn_from_events,
)
from engine.domain import AgentId, AgentProfile, AgentRunId, Message, Role, ToolSpec, WorkspaceId
from engine.runtime import (
    AGENT_PROTOCOL_DIAGNOSTIC_LOG,
    GRANTED_TOOLS_NOTE,
    with_granted_tools,
)
from engine.ports import (
    AgentRunner,
    ApprovalCapability,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    FinishReason,
    InteractiveAgentRunner,
    InteractiveMcpAgentRunner,
    McpAgentRunner,
    McpServerConfig,
    StreamingMcpAgentRunner,
    PermissionScope,
    PermissionTranslator,
    ResponseStyle,
    UserInputAnswer,
    UserInputResponse,
)

#: Captured from `claude -p --output-format stream-json --verbose --allowedTools
#: Glob Read "List the directory names under packages/adapters, then reply
#: DONE."` against Claude Code 2.1.226. Trimmed to the fields this adapter reads.
REAL_TRANSCRIPT = """\
{"type":"system","subtype":"init","cwd":"/Users/shea/code/engine",\
"session_id":"4aeecd85-e23a-48c9-87e7-938cee476896","tools":["Task","Bash","Read"]}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}
{"type":"assistant","message":{"model":"claude-opus-5","role":"assistant","content":[\
{"type":"thinking","thinking":"the user wants a listing"},\
{"type":"tool_use","id":"toolu_01WaFnohBitTsZLxaBuNg8XH","name":"Glob",\
"input":{"pattern":"packages/adapters/*"}}]}}
{"type":"user","message":{"role":"user","content":[{"tool_use_id":\
"toolu_01WaFnohBitTsZLxaBuNg8XH","type":"tool_result",\
"content":"/Users/shea/code/engine/packages/adapters/agent_runner/\\n\
/Users/shea/code/engine/packages/adapters/communications/"}]}}
{"type":"assistant","message":{"model":"claude-opus-5","role":"assistant","content":[\
{"type":"text","text":"- agent_runner\\n- communications"}]}}
{"type":"result","subtype":"success","is_error":false,"num_turns":2,\
"session_id":"4aeecd85-e23a-48c9-87e7-938cee476896","result":"- agent_runner\\n- communications",\
"usage":{"input_tokens":4,"cache_creation_input_tokens":6478,\
"cache_read_input_tokens":41156,"output_tokens":149},"total_cost_usd":0.0897}
"""

PROFILE = AgentProfile(
    agent_id=AgentId("coder"), instructions="You are terse.", description="Reads code."
)


def test_runner_satisfies_the_port() -> None:
    runner = ClaudeCodeAgentRunner()

    assert isinstance(runner, AgentRunner)
    assert isinstance(runner, InteractiveAgentRunner)
    assert isinstance(runner.permission_translator, PermissionTranslator)


def test_attribution_can_be_disabled_for_commits_and_pull_requests() -> None:
    argv = ClaudeCodeAgentRunner(attribution=False).command_line(PROFILE)

    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"attribution": {"commit": "", "pr": "", "sessionUrl": False}}


def test_response_style_selects_claude_s_own_output_style() -> None:
    argv = ClaudeCodeAgentRunner(output_style=ResponseStyle.CONCISE).command_line(PROFILE)

    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"outputStyle": "Concise"}


def test_every_provider_setting_travels_in_one_settings_document() -> None:
    """A second `--settings` would replace the first rather than add to it."""
    argv = ClaudeCodeAgentRunner(
        attribution=False, output_style=ResponseStyle.LEARNING
    ).command_line(PROFILE)

    assert argv.count("--settings") == 1
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "attribution": {"commit": "", "pr": "", "sessionUrl": False},
        "outputStyle": "Learning",
    }


def test_no_configured_style_leaves_claude_s_default_alone() -> None:
    assert "--settings" not in ClaudeCodeAgentRunner().command_line(PROFILE)


def test_every_engine_style_has_an_exactly_spelled_provider_name() -> None:
    """Claude keeps its default for a style name it does not recognize, so a
    style Engine accepts must never reach the CLI mistranslated."""
    assert set(OUTPUT_STYLES) == set(ResponseStyle)
    assert OUTPUT_STYLES[ResponseStyle.EXPLANATORY] == "Explanatory"


@pytest.mark.parametrize(
    ("kind", "tool_name", "command", "expected"),
    [
        (
            ApprovalKind.COMMAND_EXECUTION,
            "Bash",
            "uv run pytest",
            PermissionScope(ApprovalCapability.BASH, "uv run pytest"),
        ),
        (ApprovalKind.TOOL_USE, "Read", None, PermissionScope(ApprovalCapability.READ)),
        (ApprovalKind.TOOL_USE, "Glob", None, PermissionScope(ApprovalCapability.READ)),
        (ApprovalKind.TOOL_USE, "Grep", None, PermissionScope(ApprovalCapability.READ)),
        (ApprovalKind.FILE_CHANGE, "Edit", None, PermissionScope(ApprovalCapability.EDIT)),
        (ApprovalKind.FILE_CHANGE, "Write", None, PermissionScope(ApprovalCapability.EDIT)),
        (ApprovalKind.TOOL_USE, "WebFetch", None, PermissionScope(ApprovalCapability.WEB)),
        (ApprovalKind.TOOL_USE, "WebSearch", None, PermissionScope(ApprovalCapability.WEB)),
        (
            ApprovalKind.TOOL_USE,
            "mcp__github__get_issue",
            None,
            PermissionScope(ApprovalCapability.MCP),
        ),
        (ApprovalKind.TOOL_USE, "FutureTool", None, None),
    ],
)
def test_permission_translator_maps_claude_tools_to_engine_capabilities(
    kind: ApprovalKind,
    tool_name: str,
    command: str | None,
    expected: PermissionScope | None,
) -> None:
    request = ApprovalRequest(
        approval_id="provider-approval",
        kind=kind,
        tool_name=tool_name,
        command=command,
    )

    assert CLAUDE_PERMISSION_TRANSLATOR.scope_for(request) == expected


def test_capabilities_preapprove_the_tools_that_are_only_tools() -> None:
    """The other direction: a policy, as the CLI's own allow-list."""
    assert allowed_tools_for(()) == ()
    assert allowed_tools_for((ApprovalCapability.READ,)) == READ_ONLY_TOOLS
    assert allowed_tools_for(
        (ApprovalCapability.EDIT, ApprovalCapability.READ)
    ) == ("Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit")
    assert allowed_tools_for((ApprovalCapability.WEB,)) == ("WebFetch", "WebSearch")


def test_shell_and_mcp_are_never_preapproved_to_the_provider() -> None:
    """Both are still allowable -- one request at a time, through the callback.

    Preapproving `Bash` would run the commands `approvals.bash.deny` names
    before anything could consult the patterns, and an MCP grant names a server
    this list is built before knowing.
    """
    everything = allowed_tools_for(tuple(ApprovalCapability))

    assert "Bash" not in everything
    assert not any(tool.startswith("mcp__") for tool in everything)


# --- parsing ----------------------------------------------------------------


def test_parses_a_real_transcript() -> None:
    turn = turn_from_events(parse_events(REAL_TRANSCRIPT))

    assert turn.message.content == "- agent_runner\n- communications"
    assert turn.finish_reason is FinishReason.STOP


def test_tool_use_and_its_result_are_paired_by_id() -> None:
    """Claude reports tool calls structurally, so pairing is reading a field
    rather than inferring from order."""
    call, result = turn_from_events(parse_events(REAL_TRANSCRIPT)).steps

    assert call.tool_calls[0].name == "Glob"
    assert "packages/adapters/*" in call.tool_calls[0].arguments
    assert result.role is Role.TOOL
    assert result.tool_call_id == call.tool_calls[0].call_id == "toolu_01WaFnohBitTsZLxaBuNg8XH"
    assert "agent_runner" in result.content


def test_thinking_blocks_are_not_recorded() -> None:
    """The model's working, not the conversation's."""
    steps = turn_from_events(parse_events(REAL_TRANSCRIPT)).steps

    assert not any("the user wants a listing" in step.content for step in steps)


def test_fresh_written_and_read_input_are_summed() -> None:
    """Claude reports three kinds of input token. `prompt_tokens` has to mean
    the same thing it does for every other runner: all of them."""
    usage = turn_from_events(parse_events(REAL_TRANSCRIPT)).usage

    assert usage.prompt_tokens == 4 + 6478 + 41156
    assert usage.cached_prompt_tokens == 41156
    assert usage.completion_tokens == 149
    assert usage.cost_usd == pytest.approx(0.0897)


def test_recorded_actions_do_not_ask_the_caller_to_run_anything() -> None:
    turn = turn_from_events(parse_events(REAL_TRANSCRIPT))

    assert not turn.wants_tools
    assert turn.transcript == (*turn.steps, turn.message)


def test_reads_the_session_id_for_later() -> None:
    assert session_id_of(parse_events(REAL_TRANSCRIPT)) == "4aeecd85-e23a-48c9-87e7-938cee476896"


def test_a_tool_result_may_arrive_as_blocks_rather_than_a_string() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Read","input":{}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"content":[{"type":"text","text":"file body"}]}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"read it"}]}}\n'
    )

    _, result = turn_from_events(parse_events(stream)).steps

    assert result.content == "file body"


def test_a_failed_tool_result_says_so() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Read","input":{}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"is_error":true,"content":"no such file"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"it is missing"}]}}\n'
    )

    _, result = turn_from_events(parse_events(stream)).steps

    assert result.content == "error: no such file"


def test_an_errored_run_is_reported_not_raised() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"partway"}]}}\n'
        '{"type":"result","subtype":"error_max_turns","is_error":true,'
        '"result":"hit the turn limit","usage":{}}\n'
    )

    turn = turn_from_events(parse_events(stream))

    assert turn.finish_reason is FinishReason.ERROR
    assert turn.message.content == "partway"


def test_the_result_record_answers_when_nothing_else_did() -> None:
    """A turn that ends in a tool loop still reports something."""
    stream = '{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{}}'

    assert turn_from_events(parse_events(stream)).message.content == "done"


def test_no_answer_at_all_is_an_error() -> None:
    with pytest.raises(ClaudeExecutionError):
        turn_from_events(parse_events('{"type":"system","subtype":"init"}'))


# --- what this runner cannot do, said out loud ------------------------------


def test_granted_tools_are_refused_rather_than_dropped() -> None:
    runner = ClaudeCodeAgentRunner()

    with pytest.raises(ClaudeToolsUnsupportedError) as raised:
        asyncio.run(
            runner.run_turn(
                AgentRunId("ar-1"),
                PROFILE,
                (Message.user("go"),),
                tools=(ToolSpec(name="dispatch"),),
            )
        )

    assert raised.value.tool_names == ("dispatch",)


def test_a_workspace_it_cannot_resolve_is_refused() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(
            ClaudeCodeAgentRunner().run_turn(
                AgentRunId("ar-1"),
                PROFILE,
                (Message.user("go"),),
                workspace_id=WorkspaceId("ws-1"),
            )
        )


# --- invocation -------------------------------------------------------------


def test_instructions_go_to_the_system_prompt_not_the_conversation() -> None:
    """Unlike `codex exec`, this CLI has a channel for them."""
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)

    assert argv[argv.index("--append-system-prompt") + 1] == "You are terse."


def test_the_granted_tools_note_reaches_the_system_prompt() -> None:
    """This flag is the whole reason the runtime appends the note.

    `with_granted_tools` writes into `instructions`, and `instructions` reaching
    the model through this channel is what turns that into an agent that knows
    what it holds.
    """
    argv = ClaudeCodeAgentRunner().command_line(
        with_granted_tools(PROFILE, ("add_milestone",))
    )

    system_prompt = argv[argv.index("--append-system-prompt") + 1]
    assert system_prompt.startswith("You are terse.")
    assert GRANTED_TOOLS_NOTE in system_prompt
    assert "- add_milestone" in system_prompt


def test_chat_gets_read_only_tools_by_default() -> None:
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)
    allowed = argv[argv.index("--allowedTools") + 1 :]

    assert allowed == ["Read", "Glob", "Grep"]
    assert "Bash" not in allowed and "Edit" not in allowed
    assert "--dangerously-skip-permissions" not in argv


def test_workflow_write_tools_allow_edits_without_unrestricted_shell() -> None:
    argv = ClaudeCodeAgentRunner(
        allowed_tools=WORKSPACE_WRITE_TOOLS
    ).command_line(PROFILE)
    allowed = argv[argv.index("--allowedTools") + 1 :]

    assert allowed == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert "Bash" not in allowed


def test_an_empty_tool_list_omits_the_flag() -> None:
    """`--allowedTools` is variadic: passed with nothing after it, it swallows
    whatever flag comes next."""
    argv = ClaudeCodeAgentRunner(allowed_tools=()).command_line(PROFILE)

    assert "--allowedTools" not in argv


def test_a_profile_may_choose_its_model() -> None:
    profile = AgentProfile(agent_id=AgentId("coder"), instructions="", model="claude-opus-5")

    argv = ClaudeCodeAgentRunner().command_line(profile)

    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert "--append-system-prompt" not in argv, "no instructions, no flag"


def test_stream_json_needs_verbose() -> None:
    """The CLI rejects the combination without it, and the failure is opaque."""
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)

    assert argv[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]


# --- interactive control protocol ------------------------------------------


CONTROL_APPROVAL = {
    "type": "control_request",
    "request_id": "permission-1",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "input": {"command": "touch output.txt", "description": "create output"},
        "tool_use_id": "toolu_1",
        "title": "Claude wants to create output.txt",
        "permission_suggestions": [
            {
                "type": "addRules",
                "rules": [{"toolName": "Bash", "ruleContent": "touch output.txt"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ],
    },
}


def test_control_permission_is_normalized_without_a_decline_choice() -> None:
    request = approval_request_from_control(CONTROL_APPROVAL)

    assert request is not None
    assert request.approval_id == "toolu_1"
    assert request.kind is ApprovalKind.COMMAND_EXECUTION
    assert request.command == "touch output.txt"
    assert request.allowed_decisions == (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.ACCEPT_FOR_SESSION,
        ApprovalDecision.CANCEL,
    )


def test_control_permission_names_the_call_the_transcript_records() -> None:
    """What lets a client show the request beside the command it is about.

    Asserted as one identity rather than as a literal, because the id being
    right is not the property that matters: both halves have to spell the same
    `tool_use` block the same way, or the pairing silently finds nothing.
    """
    transcript = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use",'
        '"id":"toolu_1","name":"Bash","input":{"command":"touch output.txt"}}]}}\n'
        '{"type":"result","subtype":"success","is_error":false,"result":"done"}'
    )
    call = turn_from_events(parse_events(transcript)).steps[0]
    request = approval_request_from_control(CONTROL_APPROVAL)

    assert request is not None
    assert request.tool_call_id == call.tool_calls[0].call_id == "toolu_1"


def test_control_permission_without_a_tool_use_id_names_no_call() -> None:
    """Nothing to pair it with, and nothing invented: it belongs to the turn."""
    message = {**CONTROL_APPROVAL, "request": dict(CONTROL_APPROVAL["request"])}
    del message["request"]["tool_use_id"]

    request = approval_request_from_control(message)

    assert request is not None
    assert request.approval_id == "permission-1"
    assert request.tool_call_id is None


def test_control_cancel_denies_and_interrupts_the_whole_turn() -> None:
    response = control_response_for(CONTROL_APPROVAL, ApprovalDecision.CANCEL)

    assert response["response"]["response"] == {
        "behavior": "deny",
        "message": "Cancelled by user",
        "interrupt": True,
    }


def test_control_session_approval_never_writes_provider_settings() -> None:
    response = control_response_for(CONTROL_APPROVAL, ApprovalDecision.ACCEPT_FOR_SESSION)
    permission = response["response"]["response"]["updatedPermissions"][0]

    assert permission["destination"] == "session"


def test_control_question_is_structured_and_returns_human_answers() -> None:
    message = {
        "type": "control_request",
        "request_id": "question-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "AskUserQuestion",
            "tool_use_id": "toolu-question",
            "input": {
                "questions": [{
                    "header": "API",
                    "question": "Which API should remain stable?",
                    "options": [
                        {"label": "Public", "description": "Keep the public API"},
                        {"label": "Internal", "description": "Keep the internal API"},
                    ],
                    "multiSelect": False,
                }]
            },
        },
    }

    request = approval_request_from_control(message)

    assert request is not None
    assert request.kind is ApprovalKind.USER_INPUT
    assert request.requires_human is True
    assert request.questions[0].question_id == "Which API should remain stable?"
    assert [option.label for option in request.questions[0].options] == [
        "Public", "Internal"
    ]
    response = control_response_for(
        message,
        UserInputResponse((UserInputAnswer(request.questions[0].question_id, ("Public",)),)),
    )
    assert response["response"]["response"]["updatedInput"]["answers"] == {
        "Which API should remain stable?": "Public"
    }


def test_exit_plan_mode_always_requires_a_human_decision() -> None:
    message = {
        "type": "control_request",
        "request_id": "plan-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "ExitPlanMode",
            "input": {"plan": "1. Add the endpoint\n2. Test it"},
        },
    }

    request = approval_request_from_control(message)

    assert request is not None
    assert request.kind is ApprovalKind.PLAN_APPROVAL
    assert request.requires_human is True
    assert request.allowed_decisions == (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.CANCEL,
    )


def _fake_interactive_claude(tmp_path) -> str:
    binary = tmp_path / "claude"
    binary.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import json
            import sys

            def receive():
                return json.loads(sys.stdin.readline())

            def send(message):
                print(json.dumps(message), flush=True)

            initialize = receive()
            send({"type": "control_response", "response": {
                "subtype": "success", "request_id": initialize["request_id"],
                "response": {"commands": []}}})
            user = receive()
            assert user["type"] == "user"
            send({"type": "system", "subtype": "init", "session_id": "session-1"})
            send({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "toolu_1", "name": "Bash",
                "input": {"command": "touch output.txt"}}]}})
            send({"type": "control_request", "request_id": "permission-1", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash",
                "input": {"command": "touch output.txt"}, "tool_use_id": "toolu_1",
                "permission_suggestions": [{"type": "addRules", "rules": [{
                    "toolName": "Bash", "ruleContent": "touch output.txt"}],
                    "behavior": "allow", "destination": "localSettings"}]}})
            approval = receive()["response"]["response"]
            destination = approval["updatedPermissions"][0]["destination"]
            send({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "toolu_1",
                "content": approval["behavior"] + ":" + destination}]}})
            send({"type": "assistant", "message": {"content": [{
                "type": "text", "text": "done"}]}})
            send({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "usage": {}})
            '''
        )
    )
    binary.chmod(0o755)
    return str(binary)


def _fake_unknown_control_request_claude(tmp_path) -> str:
    binary = tmp_path / "claude-unknown-control"
    binary.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import json
            import sys

            def receive():
                return json.loads(sys.stdin.readline())

            def send(message):
                print(json.dumps(message), flush=True)

            initialize = receive()
            send({"type": "control_response", "response": {
                "subtype": "success", "request_id": initialize["request_id"],
                "response": {}}})
            receive()
            send({"type": "control_request", "request_id": "future-1",
                  "request": {"subtype": "future_interaction"}})
            rejection = receive()
            assert rejection["type"] == "control_response"
            assert rejection["response"]["subtype"] == "error"
            assert rejection["response"]["request_id"] == "future-1"
            error = rejection["response"]["error"]
            assert "future_interaction" in error
            assert "unsupported_subtype" in error
            assert "requested tool did not run" in error
            assert "do not retry" in error
            send({"type": "assistant", "message": {"content": [{
                "type": "text", "text": "recovered"}]}})
            send({"type": "result", "subtype": "success", "is_error": False,
                  "result": "recovered", "usage": {}})
            '''
        )
    )
    binary.chmod(0o755)
    return str(binary)


def test_interactive_turn_round_trips_a_control_approval(tmp_path) -> None:
    runner = ClaudeCodeAgentRunner(binary_path=_fake_interactive_claude(tmp_path))
    observed: list[Message] = []
    approvals = []

    async def approve(request):
        approvals.append(request)
        return ApprovalDecision.ACCEPT_FOR_SESSION

    turn = asyncio.run(
        runner.run_turn_interactive(
            AgentRunId("ar-1"),
            PROFILE,
            (Message.user("create it"),),
            approve,
            on_message=observed.append,
        )
    )

    assert [request.command for request in approvals] == ["touch output.txt"]
    assert turn.message.content == "done"
    assert turn.steps[1].content == "allow:session"
    assert observed == list(turn.transcript)
    argv = runner.interactive_command_line(PROFILE)
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"


def test_interactive_diagnostic_uses_shared_redacted_vocabulary(
    tmp_path, monkeypatch
) -> None:
    diagnostic = tmp_path / "agent-protocol.jsonl"
    monkeypatch.setenv(AGENT_PROTOCOL_DIAGNOSTIC_LOG, str(diagnostic))
    runner = ClaudeCodeAgentRunner(binary_path=_fake_interactive_claude(tmp_path))

    async def approve(_request):
        return ApprovalDecision.ACCEPT_FOR_SESSION

    asyncio.run(
        runner.run_turn_interactive(
            AgentRunId("ar-sensitive"),
            PROFILE,
            (Message.user("a prompt that must not be logged"),),
            approve,
        )
    )

    text = diagnostic.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["event"] for record in records] == [
        "session_started",
        "session_initialized",
        "interaction_received",
        "interaction_normalized",
        "interaction_response_sent",
    ]
    assert all(record["runner"] == "claude_code" for record in records)
    assert records[2]["subtype"] == "can_use_tool"
    assert records[2]["tool_name"] == "Bash"
    assert "touch output.txt" not in text
    assert "a prompt that must not be logged" not in text


def test_unknown_control_request_is_rejected_without_ending_the_turn(tmp_path) -> None:
    runner = ClaudeCodeAgentRunner(
        binary_path=_fake_unknown_control_request_claude(tmp_path)
    )

    turn = asyncio.run(
        runner.run_turn_interactive(
            AgentRunId("ar-unknown"),
            PROFILE,
            (Message.user("go"),),
            lambda _request: None,
        )
    )

    assert turn.message.content == "recovered"


def test_terminal_mcp_configuration_is_passed_to_claude() -> None:
    server = McpServerConfig("workflow", "/usr/bin/python3", ("-m", "terminal"))
    runner = ClaudeCodeAgentRunner()
    argv = runner.command_line(PROFILE, mcp_server=server)

    assert isinstance(runner, McpAgentRunner)
    assert isinstance(runner, StreamingMcpAgentRunner)
    assert isinstance(runner, InteractiveMcpAgentRunner)
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config == {
        "mcpServers": {
            "workflow": {
                "command": "/usr/bin/python3",
                "args": ["-m", "terminal"],
            }
        }
    }
    allowed = argv[argv.index("--allowedTools") + 1 :]
    # This must reach the control callback; putting it in --allowedTools would
    # auto-approve it before Engine could collect the answers.
    assert "AskUserQuestion" not in allowed
    assert "mcp__workflow__clarify" in allowed
    assert "mcp__workflow__complete_step" in allowed
    assert "mcp__workflow__fail_step" in allowed
    # Reporting progress is not an action to be approved one line at a time.
    assert "mcp__workflow__update_status" in allowed
    interactive = runner.interactive_command_line(PROFILE, server)
    assert json.loads(interactive[interactive.index("--mcp-config") + 1]) == config


def test_profile_mcp_capabilities_are_allowed_for_claude() -> None:
    server = McpServerConfig("workflow", "/usr/bin/python3", ("-m", "terminal"))
    reviewer = AgentProfile(
        agent_id=AgentId("reviewer"),
        instructions="Review the change.",
        capabilities=("add_comment",),
    )

    argv = ClaudeCodeAgentRunner().command_line(reviewer, mcp_server=server)
    allowed = argv[argv.index("--allowedTools") + 1 :]

    assert "mcp__workflow__add_comment" in allowed


# --- how long a turn may take -----------------------------------------------


def test_completed_messages_stream_in_transcript_order(tmp_path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(REAL_TRANSCRIPT)
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\ncat >/dev/null\ncat {transcript}\n")
    binary.chmod(0o755)
    runner = ClaudeCodeAgentRunner(binary_path=str(binary))
    observed: list[Message] = []

    turn = asyncio.run(
        runner.run_turn_streamed(
            AgentRunId("ar-1"), PROFILE, (Message.user("go"),), observed.append
        )
    )

    assert observed == list(turn.transcript)


def test_a_jsonl_event_may_exceed_the_stream_reader_line_limit(tmp_path) -> None:
    """One `Read` of a source file is one tool_result on one line, and that runs
    past `StreamReader.readline`'s 64 KiB well before the file looks large."""
    output = "x" * (70 * 1024)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_1",'
        '"type":"tool_result","content":' + json.dumps(output) + "}]}}\n"
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"done"}]}}\n'
    )
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\ncat >/dev/null\ncat {transcript}\n")
    binary.chmod(0o755)
    runner = ClaudeCodeAgentRunner(binary_path=str(binary))

    turn = asyncio.run(runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("go"),)))

    assert turn.steps[0].content == output
    assert turn.message.content == "done"


def test_a_turn_is_given_no_deadline_by_default(tmp_path, monkeypatch) -> None:
    """Same reasoning as the Codex runner's: a long turn is a large task, and
    `cancel` is how one ends early."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(REAL_TRANSCRIPT)
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\ncat >/dev/null\ncat {transcript}\n")
    binary.chmod(0o755)
    deadlines: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    runner = ClaudeCodeAgentRunner(binary_path=str(binary))

    turn = asyncio.run(runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("hi"),)))

    assert deadlines == [None]
    assert turn.message.content == "- agent_runner\n- communications"
