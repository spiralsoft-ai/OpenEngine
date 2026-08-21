"""The built-in implementation -> review -> human decision workflow.

This is deliberately one hardcoded, versioned state machine.  Profiles,
prompts, identifiers, and transitions live together so replacing it with a
configuration-backed workflow later does not change event or command contracts.
"""

from dataclasses import replace

from engine.domain.agents import AgentProfile
from engine.domain.commands import (
    Command,
    ProvisionWorkspace,
    RequestHumanReview,
    StartAgentRun,
)
from engine.domain.events import (
    AgentRunCompleted,
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
    IMPLEMENTATION_REVIEW_WORKFLOW_ID,
    AgentId,
    AgentInstanceId,
    AgentRunId,
    RunId,
    StepId,
)
from engine.domain.state import RunPhase, RunState
from engine.domain.workflow import StepOutput, StepSpec

WORKFLOW_ID = IMPLEMENTATION_REVIEW_WORKFLOW_ID
WORKFLOW_BASE_REF = "origin/main"

IMPLEMENTATION_STEP = StepId("implementation")
REVIEW_STEP = StepId("review")
HUMAN_REVIEW_STEP = StepId("human-review")

IMPLEMENTATION_PROFILE = AgentProfile(
    agent_id=AgentId("implementation-agent"),
    instructions=(
        "Implement the requested change in the provided workspace. Read the code "
        "before editing. Engine has already based the workspace on the current remote "
        "main commit; do not fetch, pull, or merge main before editing. Make the "
        "smallest complete change and report the result."
    ),
    capabilities=(),
    description="Implements the requested repository change.",
)

WORKFLOW_NAMING_PROFILE = replace(
    IMPLEMENTATION_PROFILE,
    instructions=(
        "Give a submitted workflow a concise display name. Do not inspect or change "
        "the workspace and do not perform the requested task."
    ),
    description="Names a submitted implementation workflow.",
)
WORKFLOW_NAME_PROMPT = (
    "Name this workflow based on the task above. Do not perform the task or use "
    "tools. Reply with only a concise name of at most eight words, with no quotes "
    "or ending punctuation."
)

REVIEW_PROFILE = AgentProfile(
    agent_id=AgentId("review-agent"),
    instructions=(
        "Review the implementation already made in the provided workspace. Read the "
        "changed code and the code around it before judging it, and check "
        "correctness, regressions the change could cause, and tests that should "
        "exist but do not. Inspect the workspace only: do not edit, revert, "
        "commit, or otherwise modify anything, and do not fix what you find. "
        "Report every finding with the file it is in and why it matters, and say "
        "so explicitly when you find nothing. Leave every finding on the pull "
        "request with the add_comment MCP tool, using file and line for inline "
        "comments when possible; if there are no findings, leave one general "
        "comment saying so. Complete the step with your findings "
        "even when they are serious; fail it only when the review itself could not "
        "be carried out. The human reviewer decides what happens to this run -- you "
        "do not approve or reject it."
    ),
    capabilities=("add_comment",),
    # Left to the runner's default on purpose. A run picks its provider, and the
    # reviewer runs on the same one, so no single model name here would be
    # correct for both Codex and Claude Code.
    model="",
    description="Inspects an implementation without modifying it.",
)

IMPLEMENTATION_STEP_SPEC = StepSpec(
    step_id=IMPLEMENTATION_STEP,
    agent_id=IMPLEMENTATION_PROFILE.agent_id,
    required_outputs=("pr_url",),
    editable=True,
)
REVIEW_STEP_SPEC = StepSpec(
    step_id=REVIEW_STEP,
    agent_id=REVIEW_PROFILE.agent_id,
    # The human decision needs the findings themselves, not a reviewer's
    # one-line verdict on them, so they are a declared output the terminal tool
    # refuses to complete the step without.
    required_outputs=("findings",),
)


def agent_instance_id(run_id: RunId, step_id: StepId) -> AgentInstanceId:
    """Return the stable agent-instance id for one workflow step."""

    return AgentInstanceId(f"{run_id}:{step_id}:instance")


def agent_run_id(run_id: RunId, step_id: StepId) -> AgentRunId:
    """Return the stable agent-run id for one workflow step."""

    return AgentRunId(f"{run_id}:{step_id}:run")


def _next_agent_run_id(state: RunState, step_id: StepId) -> AgentRunId:
    """Return a deterministic new execution id for a step being run again."""

    base = agent_run_id(state.run_id, step_id)
    attempts = sum(
        existing == base or str(existing).startswith(f"{base}:")
        for existing in state.agent_runs
    )
    return base if attempts == 0 else AgentRunId(f"{base}:{attempts + 1}")


def implementation_prompt(task_prompt: str) -> str:
    """The implementation receives the original task without rewriting it."""

    return task_prompt


def _outputs_text(outputs: tuple[StepOutput, ...]) -> str:
    if not outputs:
        return "(none)"
    return "\n".join(f"- {output.name}: {output.value}" for output in outputs)


def review_prompt(task_prompt: str, implementation: StepCompleted) -> str:
    """Build reviewer context from the task and structured implementation result."""

    return (
        "Inspect the workspace but do not modify it. Review the implementation "
        "below against the original task and report what a human deciding this "
        "run needs to know, in the `findings` output.\n\n"
        f"Original task:\n{task_prompt}\n\n"
        "Implementation result:\n"
        f"Outcome: {implementation.outcome}\n"
        f"Summary: {implementation.summary}\n"
        f"Outputs:\n{_outputs_text(implementation.outputs)}"
    )


def human_review_summary(
    implementation: StepCompleted, review: StepCompleted
) -> str:
    """Present both agent results without treating the review outcome as a decision."""

    return (
        "Implementation:\n"
        f"{implementation.summary}\n"
        f"Outputs:\n{_outputs_text(implementation.outputs)}\n\n"
        "Review:\n"
        f"Outcome: {review.outcome}\n"
        f"{review.summary}\n"
        f"Outputs:\n{_outputs_text(review.outputs)}"
    )


def _start_agent(
    state: RunState,
    *,
    step: StepSpec,
    profile: AgentProfile,
    prompt: str,
) -> StartAgentRun:
    execution_id = (
        state.current_agent_run_id
        if state.current_step_id == step.step_id
        and state.current_agent_run_id is not None
        else agent_run_id(state.run_id, step.step_id)
    )
    return StartAgentRun(
        run_id=state.run_id,
        agent_run_id=execution_id,
        instance_id=agent_instance_id(state.run_id, step.step_id),
        profile=profile,
        prompt=prompt,
        workspace_id=state.workspace_id,
        step=step,
    )


def start_review_command(
    state: RunState, implementation: StepCompleted
) -> StartAgentRun:
    """Recreate the deterministic review command from durable run state.

    The runtime uses this after a process restart: the implementation result is
    durable, while the command that was emitted from it was only ever in memory.
    """

    return _start_agent(
        state,
        step=REVIEW_STEP_SPEC,
        profile=REVIEW_PROFILE,
        prompt=review_prompt(state.prompt, implementation),
    )


def start_implementation_command(state: RunState) -> StartAgentRun:
    """Recreate the active implementation command for a conversational turn."""

    return _start_agent(
        state,
        step=IMPLEMENTATION_STEP_SPEC,
        profile=IMPLEMENTATION_PROFILE,
        prompt=implementation_prompt(state.prompt),
    )


def _matches_expected_step(state: RunState, event: StepCompleted) -> bool:
    return (
        event.step_id == state.current_step_id
        and event.agent_run_id == state.current_agent_run_id
    )


def decide_implementation_review(
    state: RunState, event: Event
) -> tuple[RunState, tuple[Command, ...]]:
    """Fold one event through the implementation-review-v1 state machine."""

    match event:
        case RunRequested() if state.phase is RunPhase.PENDING:
            next_state = RunState(
                run_id=event.run_id,
                task_id=event.task_id,
                workflow_id=event.workflow_id,
                phase=RunPhase.PREPARING_WORKSPACE,
                repository=event.repository,
                prompt=event.prompt,
                max_agent_runs=state.max_agent_runs,
            )
            command = ProvisionWorkspace(
                run_id=event.run_id,
                repository=event.repository,
                base_ref=WORKFLOW_BASE_REF,
            )
            return next_state, (command,)

        case WorkspaceProvisioned() if state.phase is RunPhase.PREPARING_WORKSPACE:
            expected_run_id = agent_run_id(state.run_id, IMPLEMENTATION_STEP)
            next_state = replace(
                state,
                phase=RunPhase.IMPLEMENTING,
                workspace_id=event.workspace_id,
                current_step_id=IMPLEMENTATION_STEP,
                current_agent_run_id=expected_run_id,
                agent_runs=(*state.agent_runs, expected_run_id),
            )
            command = _start_agent(
                next_state,
                step=IMPLEMENTATION_STEP_SPEC,
                profile=IMPLEMENTATION_PROFILE,
                prompt=implementation_prompt(state.prompt),
            )
            return next_state, (command,)

        case RunNamed():
            return replace(state, name=event.name), ()

        case StepReactivated() if (
            event.step_id == IMPLEMENTATION_STEP
            and state.phase
            in (
                RunPhase.REVIEWING,
                RunPhase.AWAITING_HUMAN_REVIEW,
                RunPhase.SUCCEEDED,
                RunPhase.FAILED,
            )
        ):
            expected_run_id = _next_agent_run_id(state, IMPLEMENTATION_STEP)
            return replace(
                state,
                phase=RunPhase.IMPLEMENTING,
                current_step_id=IMPLEMENTATION_STEP,
                current_agent_run_id=expected_run_id,
                agent_runs=(*state.agent_runs, expected_run_id),
                step_results=(),
                human_review=None,
                failure_reason="",
            ), ()

        case StepCompleted() if (
            state.phase is RunPhase.IMPLEMENTING
            and _matches_expected_step(state, event)
        ):
            if event.outcome != "success":
                return replace(
                    state,
                    phase=RunPhase.FAILED,
                    step_results=(*state.step_results, event),
                ), ()

            expected_run_id = _next_agent_run_id(state, REVIEW_STEP)
            next_state = replace(
                state,
                phase=RunPhase.REVIEWING,
                current_step_id=REVIEW_STEP,
                current_agent_run_id=expected_run_id,
                step_results=(*state.step_results, event),
                agent_runs=(*state.agent_runs, expected_run_id),
            )
            command = start_review_command(next_state, event)
            return next_state, (command,)

        case StepCompleted() if (
            state.phase is RunPhase.REVIEWING
            and _matches_expected_step(state, event)
        ):
            implementation = next(
                (
                    result
                    for result in reversed(state.step_results)
                    if result.step_id == IMPLEMENTATION_STEP
                ),
                None,
            )
            if implementation is None:
                # Replayed state should have this result. Treat malformed or
                # partially migrated state as a no-op instead of inventing
                # reviewer context or making the pure reducer raise.
                return state, ()
            next_state = replace(
                state,
                phase=RunPhase.AWAITING_HUMAN_REVIEW,
                current_step_id=HUMAN_REVIEW_STEP,
                current_agent_run_id=None,
                step_results=(*state.step_results, event),
            )
            command = RequestHumanReview(
                run_id=state.run_id,
                step_id=HUMAN_REVIEW_STEP,
                title=f"Review implementation for {state.task_id}",
                summary=human_review_summary(implementation, event),
            )
            return next_state, (command,)

        case HumanReviewCompleted() if (
            state.phase is RunPhase.AWAITING_HUMAN_REVIEW
            and event.step_id == state.current_step_id
        ):
            phase = RunPhase.SUCCEEDED if event.approved else RunPhase.FAILED
            return replace(state, phase=phase, human_review=event), ()

        case AgentRunCompleted() if (
            state.phase in (RunPhase.IMPLEMENTING, RunPhase.REVIEWING)
            and event.agent_run_id == state.current_agent_run_id
            and not event.succeeded
        ):
            return replace(state, phase=RunPhase.FAILED, failure_reason=event.summary), ()

        case RunFailed():
            return replace(state, phase=RunPhase.FAILED, failure_reason=event.reason), ()

        case _:
            return state, ()


__all__ = [
    "HUMAN_REVIEW_STEP",
    "IMPLEMENTATION_PROFILE",
    "IMPLEMENTATION_STEP",
    "IMPLEMENTATION_STEP_SPEC",
    "REVIEW_PROFILE",
    "REVIEW_STEP",
    "REVIEW_STEP_SPEC",
    "WORKFLOW_ID",
    "WORKFLOW_BASE_REF",
    "WORKFLOW_NAME_PROMPT",
    "WORKFLOW_NAMING_PROFILE",
    "agent_instance_id",
    "agent_run_id",
    "decide_implementation_review",
    "human_review_summary",
    "implementation_prompt",
    "review_prompt",
    "start_implementation_command",
    "start_review_command",
]
