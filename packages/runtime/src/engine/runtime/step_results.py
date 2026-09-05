"""Workflow terminal-tool instructions and argument validation."""

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from engine.domain import (
    AgentRunId,
    RunFailed,
    RunId,
    Message,
    Role,
    StepCompleted,
    StepOutput,
    StepSpec,
    ToolCall,
    ToolParameter,
    ToolParameterType,
    ToolSpec,
)
from engine.ports import AgentTurn


class InvalidStepResultError(ValueError):
    """A terminal agent turn does not contain a valid workflow result."""


INVALID_COMPLETION_ERROR = (
    "Valid completion states are from completing, failing, or otherwise "
    "requesting clarification on a step"
)

_CLARIFICATION_OR_ESCALATION_TOOLS = frozenset(
    {
        "askuserquestion",
        "clarify",
        "escalate",
        "escalatetohuman",
        "requestclarification",
        "requesthumanreview",
        "requestuserinput",
    }
)


def step_result_instructions(step: StepSpec, *, status_updates: bool = False) -> str:
    """Tell the agent which tool calls may end or pause this step."""
    outputs = ", ".join(step.required_outputs) or "none"
    # Said here as well as in the tool's own description because a step is told
    # what ends it in this note, and a reporting tool introduced only in the
    # listing reads as optional decoration next to that.
    reporting = (
        "\n\nThis work order was requested in a conversation, and the person who "
        "asked is watching it there. Call `update_status` as you go -- when you "
        "have understood the request, when you start making the change, and "
        "whenever something happens they would want to know about. It does not "
        "end the step."
        if status_updates
        else ""
    )
    return (
        "When this step is complete, call the workflow MCP tool `complete_step`. "
        "If you are only answering a human's question and did not change the "
        "implementation, call `clarify` after answering; it leaves workflow run "
        "state unchanged. If the step cannot be completed, call `fail_step`. "
        "If you need information "
        "or a human decision before continuing, use an available clarification or "
        "escalation tool. Do not end the session without making one of those calls."
        f"{reporting}\n\n"
        f"The declared outputs for `complete_step` are: {outputs}."
    )


def requests_clarification_or_escalation(turn: AgentTurn) -> bool:
    """Whether a provider tool call validly pauses an unfinished step."""
    return _messages_request_clarification_or_escalation(turn.transcript)


def awaits_human_answer(turn: AgentTurn) -> bool:
    """Whether this turn stopped on a question rather than on `clarify`.

    Both end the turn the same way, and both are valid, but they are opposite
    things to say to a person watching: one is waiting on them, and the other
    is the agent reporting that it answered and changed nothing.
    """
    return any(
        name != "clarify" for name in _clarification_tools(turn.transcript)
    )


def latest_turn_requests_clarification_or_escalation(
    messages: Sequence[Message],
) -> bool:
    """Whether the conversation's latest agent turn paused for a human."""
    last_user = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role is Role.USER
        ),
        -1,
    )
    return _messages_request_clarification_or_escalation(messages[last_user + 1 :])


def _messages_request_clarification_or_escalation(
    messages: Sequence[Message],
) -> bool:
    return bool(_clarification_tools(messages))


def _clarification_tools(messages: Sequence[Message]) -> frozenset[str]:
    """Which pausing tools these messages called, by their normalized name."""
    called = set()
    for message in messages:
        for call in message.tool_calls:
            leaf_name = call.name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
            normalized = "".join(
                character
                for character in leaf_name.lower()
                if character.isalnum()
            )
            if normalized in _CLARIFICATION_OR_ESCALATION_TOOLS:
                called.add(normalized)
    return frozenset(called)


def complete_step_tool(step: StepSpec) -> ToolSpec:
    """Describe the provider-neutral tool for completing ``step``."""
    outputs = tuple(
        ToolParameter(
            name=name,
            description=f"Value of the required {name!r} step output.",
        )
        for name in step.required_outputs
    )
    return ToolSpec(
        name="complete_step",
        description=f"Complete workflow step {step.step_id!s} successfully.",
        parameters=(
            ToolParameter(
                name="outcome",
                description="The terminal outcome.",
                choices=("success",),
            ),
            ToolParameter(
                name="summary",
                description="A human-readable summary of what was completed.",
            ),
            ToolParameter(
                name="outputs",
                type=ToolParameterType.OBJECT,
                description="The step's declared outputs.",
                properties=outputs,
            ),
        ),
    )


def fail_step_tool(step: StepSpec) -> ToolSpec:
    """Describe the provider-neutral tool for failing ``step``."""
    return ToolSpec(
        name="fail_step",
        description=f"Fail workflow step {step.step_id!s}.",
        parameters=(
            ToolParameter(
                name="summary",
                description="Why the step could not be completed.",
            ),
        ),
    )


def _arguments_from_tool_call(call: ToolCall, expected_name: str) -> dict[str, Any]:
    if call.name != expected_name:
        raise InvalidStepResultError(
            f"expected {expected_name!r} tool call, got {call.name!r}"
        )
    try:
        arguments: Any = json.loads(call.arguments)
    except (json.JSONDecodeError, TypeError) as error:
        raise InvalidStepResultError(
            f"{expected_name} arguments are not valid JSON"
        ) from error
    if not isinstance(arguments, dict):
        raise InvalidStepResultError(f"{expected_name} arguments must be a JSON object")
    return arguments


def _validate_outputs(step: StepSpec, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InvalidStepResultError("step result outputs must be a JSON object")
    if any(not isinstance(name, str) for name in value):
        raise InvalidStepResultError("step result output names must be strings")

    expected = set(step.required_outputs)
    actual = set(value)
    missing = sorted(expected - actual)
    if missing:
        raise InvalidStepResultError(
            f"step result is missing required outputs: {', '.join(missing)}"
        )
    extra = sorted(actual - expected)
    if extra:
        raise InvalidStepResultError(
            f"step result has undeclared outputs: {', '.join(extra)}"
        )
    if any(not isinstance(output, str) for output in value.values()):
        raise InvalidStepResultError("step result output values must be strings")
    return value


def _step_completed(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    result: dict[str, Any],
) -> StepCompleted:
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        raise InvalidStepResultError("step result outcome must be a nonempty string")
    if outcome != "success":
        raise InvalidStepResultError("complete_step outcome must be 'success'")

    summary = result.get("summary")
    if not isinstance(summary, str):
        raise InvalidStepResultError("step result summary must be a string")

    outputs = _validate_outputs(step, result.get("outputs", {}))
    return StepCompleted(
        run_id=run_id,
        step_id=step.step_id,
        agent_run_id=agent_run_id,
        outcome=outcome,
        summary=summary,
        outputs=tuple(
            StepOutput(name=name, value=outputs[name]) for name in sorted(outputs)
        ),
    )


def step_completed_from_tool_call(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    call: ToolCall,
) -> StepCompleted:
    """Validate a ``complete_step`` call and return its completion event."""
    result = _arguments_from_tool_call(call, "complete_step")
    if set(result) != {"outcome", "summary", "outputs"}:
        raise InvalidStepResultError(
            "complete_step arguments must be exactly outcome, summary, and outputs"
        )
    return _step_completed(
        run_id=run_id,
        step=step,
        agent_run_id=agent_run_id,
        result=result,
    )


def run_failed_from_tool_call(*, run_id: RunId, call: ToolCall) -> RunFailed:
    """Validate a ``fail_step`` call and return its run failure event."""
    result = _arguments_from_tool_call(call, "fail_step")
    if set(result) != {"summary"}:
        raise InvalidStepResultError("fail_step requires exactly one summary argument")
    summary = result["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidStepResultError("fail_step summary must be a nonempty string")
    return RunFailed(run_id=run_id, reason=summary)


def step_result_from_tool_call(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    call: ToolCall,
) -> StepCompleted | RunFailed:
    """Parse either terminal workflow tool call into its domain event."""
    if call.name == "complete_step":
        return step_completed_from_tool_call(
            run_id=run_id,
            step=step,
            agent_run_id=agent_run_id,
            call=call,
        )
    if call.name == "fail_step":
        return run_failed_from_tool_call(run_id=run_id, call=call)
    raise InvalidStepResultError(f"unknown step result tool: {call.name!r}")


def step_completed_from_arguments(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    arguments: object,
    mcp_request_id: str | int,
) -> StepCompleted:
    """Validate bound MCP arguments with the canonical terminal-tool parser."""
    if not isinstance(arguments, dict):
        raise InvalidStepResultError("step result must be a JSON object")
    event = step_completed_from_tool_call(
        run_id=run_id,
        step=step,
        agent_run_id=agent_run_id,
        call=ToolCall(
            call_id=str(mcp_request_id),
            name="complete_step",
            arguments=json.dumps(arguments),
        ),
    )
    return replace(event, mcp_request_id=mcp_request_id)


def run_failed_from_arguments(
    *,
    run_id: RunId,
    agent_run_id: AgentRunId,
    arguments: object,
    mcp_request_id: str | int,
) -> RunFailed:
    """Validate a bound `fail_step` call and construct its runtime event."""
    if not isinstance(arguments, dict):
        raise InvalidStepResultError("failure result must be a JSON object")
    event = run_failed_from_tool_call(
        run_id=run_id,
        call=ToolCall(
            call_id=str(mcp_request_id),
            name="fail_step",
            arguments=json.dumps(arguments),
        ),
    )
    return replace(
        event,
        agent_run_id=agent_run_id,
        mcp_request_id=mcp_request_id,
    )


__all__ = [
    "INVALID_COMPLETION_ERROR",
    "InvalidStepResultError",
    "awaits_human_answer",
    "complete_step_tool",
    "fail_step_tool",
    "latest_turn_requests_clarification_or_escalation",
    "run_failed_from_tool_call",
    "run_failed_from_arguments",
    "requests_clarification_or_escalation",
    "step_completed_from_arguments",
    "step_completed_from_tool_call",
    "step_result_instructions",
    "step_result_from_tool_call",
]
