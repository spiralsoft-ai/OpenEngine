"""Focused tests for the built-in implementation-review-v1 reducer."""

from pathlib import Path

import pytest

from engine.core import decide
from engine.core.decide import WORKFLOW_DECIDERS
from engine.core.workflows.implementation_review import (
    HUMAN_REVIEW_STEP,
    IMPLEMENTATION_STEP,
    IMPLEMENTATION_STEP_SPEC,
    REVIEW_STEP,
    REVIEW_STEP_SPEC,
    WORKFLOW_ID,
)
from engine.domain import (
    AgentRunCompleted,
    AgentRunId,
    HumanReviewCompleted,
    ProvisionWorkspace,
    RequestHumanReview,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    StepReactivated,
    StepId,
    StepOutput,
    TaskId,
    WorkspaceId,
    WorkspaceProvisioned,
)

RUN_ID = RunId("run-42")
TASK_ID = TaskId("task-7")
WORKSPACE_ID = WorkspaceId("workspace-42")
TASK_PROMPT = "Fix the race and add a regression test."


def test_only_the_implementation_step_has_an_editable_conversation() -> None:
    assert IMPLEMENTATION_STEP_SPEC.editable is True
    assert REVIEW_STEP_SPEC.editable is False


def pending_state() -> RunState:
    return RunState(run_id=RUN_ID, task_id=TASK_ID)


def requested() -> tuple[RunState, ProvisionWorkspace]:
    state, commands = decide(
        pending_state(),
        RunRequested(
            run_id=RUN_ID,
            task_id=TASK_ID,
            prompt=TASK_PROMPT,
            repository="acme/api",
        ),
    )
    assert len(commands) == 1
    assert isinstance(commands[0], ProvisionWorkspace)
    return state, commands[0]


def implementing() -> tuple[RunState, StartAgentRun]:
    state, _ = requested()
    state, commands = decide(
        state,
        WorkspaceProvisioned(
            run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            root_path="/tmp/workspace-42",
        ),
    )
    assert len(commands) == 1
    assert isinstance(commands[0], StartAgentRun)
    return state, commands[0]


def implementation_result(
    *,
    outcome: str = "success",
    step_id: StepId = IMPLEMENTATION_STEP,
    agent_run_id: AgentRunId = AgentRunId("run-42:implementation:run"),
) -> StepCompleted:
    return StepCompleted(
        run_id=RUN_ID,
        step_id=step_id,
        agent_run_id=agent_run_id,
        outcome=outcome,
        summary="Fixed the race with a lock and added a regression test.",
        outputs=(
            StepOutput(name="pr_url", value="https://github.com/acme/api/pull/42"),
        ),
    )


def reviewing() -> tuple[RunState, StartAgentRun, StepCompleted]:
    state, _ = implementing()
    result = implementation_result()
    state, commands = decide(state, result)
    assert len(commands) == 1
    assert isinstance(commands[0], StartAgentRun)
    return state, commands[0], result


def review_result(*, outcome: str = "success") -> StepCompleted:
    return StepCompleted(
        run_id=RUN_ID,
        step_id=REVIEW_STEP,
        agent_run_id=AgentRunId("run-42:review:run"),
        outcome=outcome,
        summary="The fix is focused and the regression test covers the race.",
    )


def awaiting_human(
    *, review_outcome: str = "success"
) -> tuple[RunState, RequestHumanReview]:
    state, _, _ = reviewing()
    state, commands = decide(state, review_result(outcome=review_outcome))
    assert len(commands) == 1
    assert isinstance(commands[0], RequestHumanReview)
    return state, commands[0]


def test_request_provisions_a_workspace_and_stores_the_workflow() -> None:
    state, command = requested()

    assert state.phase is RunPhase.PREPARING_WORKSPACE
    assert state.workflow_id == WORKFLOW_ID
    assert state.repository == "acme/api"
    assert state.prompt == TASK_PROMPT
    assert command == ProvisionWorkspace(
        run_id=RUN_ID,
        repository="acme/api",
        base_ref="origin/main",
    )
    assert WORKFLOW_DECIDERS[WORKFLOW_ID]


def test_provisioning_starts_implementation_with_the_original_prompt() -> None:
    state, command = implementing()

    assert state.phase is RunPhase.IMPLEMENTING
    assert state.workspace_id == WORKSPACE_ID
    assert state.current_step_id == IMPLEMENTATION_STEP
    assert state.current_agent_run_id == AgentRunId("run-42:implementation:run")
    assert command.instance_id == "run-42:implementation:instance"
    assert command.agent_run_id == "run-42:implementation:run"
    assert command.prompt == TASK_PROMPT
    assert command.workspace_id == WORKSPACE_ID
    assert command.step is not None
    assert command.step.step_id == IMPLEMENTATION_STEP
    assert command.step.required_outputs == ("pr_url",)
    assert "do not fetch, pull, or merge main" in command.profile.instructions


def test_a_generated_name_is_recorded_without_changing_the_active_step() -> None:
    state, _ = implementing()

    named, commands = decide(
        state, RunNamed(run_id=RUN_ID, name="Fix shared counter race")
    )

    assert named.name == "Fix shared counter race"
    assert named.phase is RunPhase.IMPLEMENTING
    assert named.current_step_id == IMPLEMENTATION_STEP
    assert commands == ()


def test_implementation_success_starts_review_with_result_context() -> None:
    state, command, result = reviewing()

    assert state.phase is RunPhase.REVIEWING
    assert state.current_step_id == REVIEW_STEP
    assert state.current_agent_run_id == AgentRunId("run-42:review:run")
    assert state.step_results == (result,)
    assert command.instance_id == "run-42:review:instance"
    assert command.agent_run_id == "run-42:review:run"
    assert command.step is not None
    assert command.step.step_id == REVIEW_STEP
    assert command.profile.capabilities == ("add_comment",)
    assert TASK_PROMPT in command.prompt
    assert result.summary in command.prompt
    assert "https://github.com/acme/api/pull/42" in command.prompt
    assert "do not modify" in command.prompt


@pytest.mark.parametrize("outcome", ["failed", "changes_requested", "blocked"])
def test_implementation_non_success_fails(outcome: str) -> None:
    state, _ = implementing()

    next_state, commands = decide(state, implementation_result(outcome=outcome))

    assert next_state.phase is RunPhase.FAILED
    assert commands == ()


@pytest.mark.parametrize("outcome", ["success", "changes_requested", "failed"])
def test_any_valid_review_outcome_requests_human_review(outcome: str) -> None:
    state, _, implementation = reviewing()
    review = review_result(outcome=outcome)

    next_state, commands = decide(state, review)

    assert next_state.phase is RunPhase.AWAITING_HUMAN_REVIEW
    assert next_state.current_step_id == HUMAN_REVIEW_STEP
    assert next_state.current_agent_run_id is None
    assert next_state.step_results == (implementation, review)
    assert len(commands) == 1
    command = commands[0]
    assert isinstance(command, RequestHumanReview)
    assert command.step_id == HUMAN_REVIEW_STEP
    assert implementation.summary in command.summary
    assert review.summary in command.summary
    assert outcome in command.summary


@pytest.mark.parametrize(
    ("approved", "expected_phase"),
    [(True, RunPhase.SUCCEEDED), (False, RunPhase.FAILED)],
)
def test_human_decision_is_final(approved: bool, expected_phase: RunPhase) -> None:
    state, _ = awaiting_human()

    next_state, commands = decide(
        state,
        HumanReviewCompleted(
            run_id=RUN_ID,
            step_id=HUMAN_REVIEW_STEP,
            approved=approved,
            summary="Human decision recorded.",
        ),
    )

    assert next_state.phase is expected_phase
    assert commands == ()


@pytest.mark.parametrize("terminal", [False, True])
def test_reactivating_implementation_discards_downstream_results(
    terminal: bool,
) -> None:
    state, _ = awaiting_human()
    if terminal:
        state, _ = decide(
            state,
            HumanReviewCompleted(
                run_id=RUN_ID,
                step_id=HUMAN_REVIEW_STEP,
                approved=True,
                summary="Approved before more guidance arrived.",
            ),
        )

    next_state, commands = decide(
        state,
        StepReactivated(run_id=RUN_ID, step_id=IMPLEMENTATION_STEP),
    )

    assert next_state.phase is RunPhase.IMPLEMENTING
    assert next_state.current_step_id == IMPLEMENTATION_STEP
    assert next_state.current_agent_run_id == "run-42:implementation:run:2"
    assert next_state.agent_runs[-1] == next_state.current_agent_run_id
    assert next_state.step_results == ()
    assert next_state.human_review is None
    assert next_state.failure_reason == ""
    assert commands == ()


@pytest.mark.parametrize("stage", ["implementation", "review"])
def test_current_agent_failure_fails_the_run(stage: str) -> None:
    if stage == "implementation":
        state, _ = implementing()
    else:
        state, _, _ = reviewing()
    assert state.current_agent_run_id is not None

    next_state, commands = decide(
        state,
        AgentRunCompleted(
            run_id=RUN_ID,
            agent_run_id=state.current_agent_run_id,
            succeeded=False,
            summary="Agent process failed.",
        ),
    )

    assert next_state.phase is RunPhase.FAILED
    assert commands == ()


def test_successful_operational_completion_does_not_advance() -> None:
    state, _ = implementing()
    assert state.current_agent_run_id is not None

    next_state, commands = decide(
        state,
        AgentRunCompleted(
            run_id=RUN_ID,
            agent_run_id=state.current_agent_run_id,
            succeeded=True,
            summary="Agent process exited normally.",
        ),
    )

    assert next_state is state
    assert commands == ()


@pytest.mark.parametrize(
    "event",
    [
        implementation_result(step_id=StepId("other")),
        implementation_result(agent_run_id=AgentRunId("run-42:other:run")),
    ],
)
def test_wrong_step_or_agent_run_is_ignored(event: StepCompleted) -> None:
    state, _ = implementing()

    next_state, commands = decide(state, event)

    assert next_state is state
    assert commands == ()


def test_duplicate_and_late_completions_emit_no_duplicate_commands() -> None:
    review_state, review_command, implementation = reviewing()

    duplicate_state, duplicate_commands = decide(review_state, implementation)
    assert duplicate_state is review_state
    assert duplicate_commands == ()

    human_state, human_commands = decide(review_state, review_result())
    assert len(human_commands) == 1
    late_state, late_commands = decide(human_state, implementation)
    assert late_state is human_state
    assert late_commands == ()
    assert review_command.step is not None


def test_wrong_human_review_step_is_ignored() -> None:
    state, _ = awaiting_human()

    next_state, commands = decide(
        state,
        HumanReviewCompleted(
            run_id=RUN_ID,
            step_id=StepId("other-human-step"),
            approved=True,
        ),
    )

    assert next_state is state
    assert commands == ()


@pytest.mark.parametrize("terminal_phase", [RunPhase.SUCCEEDED, RunPhase.FAILED])
def test_terminal_states_ignore_further_events(terminal_phase: RunPhase) -> None:
    state, _ = awaiting_human()
    terminal = RunState(
        run_id=state.run_id,
        task_id=state.task_id,
        workflow_id=state.workflow_id,
        phase=terminal_phase,
        repository=state.repository,
        prompt=state.prompt,
        workspace_id=state.workspace_id,
        current_step_id=state.current_step_id,
        step_results=state.step_results,
    )

    next_state, commands = decide(terminal, review_result())

    assert next_state is terminal
    assert commands == ()


def test_generated_ids_are_deterministic_across_replay() -> None:
    first_state, first_command = implementing()
    second_state, second_command = implementing()

    assert first_state == second_state
    assert first_command == second_command
    assert first_command.instance_id == "run-42:implementation:instance"
    assert first_command.agent_run_id == "run-42:implementation:run"


def test_workflow_step_hardcodes_do_not_leak_outside_workflow_module() -> None:
    root = Path(__file__).parents[1]
    implementation = root / "packages/engine/src/engine/core/workflows/implementation_review.py"
    forbidden = ('StepId("implementation")', 'StepId("review")', 'StepId("human-review")')

    for path in (root / "packages").rglob("*.py"):
        if path == implementation:
            continue
        source = path.read_text()
        assert not any(value in source for value in forbidden), path
