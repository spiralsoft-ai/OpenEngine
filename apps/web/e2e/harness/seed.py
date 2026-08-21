"""Create the browser suite's already-populated SQLite database.

The fixture is data, not alternate application behaviour: it uses the real
SQLite state-store API and the repository's current workflow definition.  The
web server opens the completed file in a separate process afterwards, which is
the restart/cold-start path this fixture exists to cover.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRun,
    AgentRunId,
    AgentRunStatus,
    ConversationId,
    HumanReviewCompleted,
    Message,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StepCompleted,
    StepId,
    StepOutput,
    TaskId,
    WorkflowId,
)
from engine.runtime import load_workflow_catalog

REPO_ROOT = Path(__file__).resolve().parents[4]

CHAT_INSTANCE = AgentInstanceId("agi-seeded-chat")
RUN_ID = RunId("run-seeded-history")
TASK_ID = TaskId("task-seeded-history")
IMPLEMENTATION_INSTANCE = AgentInstanceId("agi-seeded-implementation")
REVIEW_INSTANCE = AgentInstanceId("agi-seeded-review")
IMPLEMENTATION_RUN = AgentRunId("ar-seeded-implementation")
REVIEW_RUN = AgentRunId("ar-seeded-review")


async def seed(path: Path, repository: str) -> None:
    store = SQLiteStateStore(path)
    try:
        await _seed_chat(store)
        await _seed_workflow(store, repository)
    finally:
        store.close()


async def _seed_chat(store: SQLiteStateStore) -> None:
    await store.create_instance(
        AgentId("coder"),
        runner="claude",
        instance_id=CHAT_INSTANCE,
        conversation_id=ConversationId("conv-seeded-chat"),
    )
    await store.update_instance_metadata(
        CHAT_INSTANCE,
        title="Seeded SQLite conversation",
        archived=False,
        runner="claude",
    )
    await store.append_messages(
        CHAT_INSTANCE,
        (
            Message.user("What survives when the web process restarts?"),
            Message.assistant("The SQLite-backed conversation history survives."),
            Message.user("Can I still navigate back to this answer?"),
            Message.assistant("Yes. This second turn proves the complete history loaded."),
        ),
    )


async def _seed_workflow(store: SQLiteStateStore, repository: str) -> None:
    definition = load_workflow_catalog(REPO_ROOT / "workflows").require(
        WorkflowId("implementation-review-v1")
    )
    implementation = StepCompleted(
        run_id=RUN_ID,
        step_id=StepId("implementation"),
        agent_run_id=IMPLEMENTATION_RUN,
        outcome="success",
        summary="Preserved every browser route.",
        outputs=(
            StepOutput("pr_url", "https://github.com/acme/engine/pull/41"),
        ),
        mcp_request_id="seed-implementation-result",
    )
    review = StepCompleted(
        run_id=RUN_ID,
        step_id=StepId("review"),
        agent_run_id=REVIEW_RUN,
        outcome="success",
        summary="Verified the navigation coverage.",
        outputs=(StepOutput("findings", "No navigation regressions found."),),
        mcp_request_id="seed-review-result",
    )
    decision = HumanReviewCompleted(
        run_id=RUN_ID,
        step_id=StepId("human-review"),
        approved=True,
        summary="Seeded run accepted after browser review.",
    )
    state = RunState(
        run_id=RUN_ID,
        task_id=TASK_ID,
        workflow_id=definition.workflow_id,
        phase=RunPhase.SUCCEEDED,
        repository=repository,
        prompt="Preserve browser navigation and durable conversation history.",
        name="Seeded navigation coverage",
        agent_runs=(IMPLEMENTATION_RUN, REVIEW_RUN),
        current_step_id=StepId("human-review"),
        runner_name="codex",
        step_results=(implementation, review),
        human_review=decision,
        human_reviews=(decision,),
        workflow_definition=definition,
    )
    await store.save(state)
    await store.append_events(
        RUN_ID,
        (
            RunRequested(
                run_id=RUN_ID,
                task_id=TASK_ID,
                prompt=state.prompt,
                repository=repository,
                workflow_id=definition.workflow_id,
            ),
            RunNamed(run_id=RUN_ID, name=state.name),
            implementation,
            review,
            decision,
        ),
    )
    await _seed_step_conversation(
        store,
        instance_id=IMPLEMENTATION_INSTANCE,
        conversation_id=ConversationId("conv-seeded-implementation"),
        step_id=StepId("implementation"),
        agent_id=AgentId("implementation-agent"),
        agent_run_id=IMPLEMENTATION_RUN,
        title="Seeded implementation conversation",
        user_text="Preserve browser navigation and durable conversation history.",
        assistant_text="I preserved the routes and added durable history coverage.",
        summary=implementation.summary,
    )
    await _seed_step_conversation(
        store,
        instance_id=REVIEW_INSTANCE,
        conversation_id=ConversationId("conv-seeded-review"),
        step_id=StepId("review"),
        agent_id=AgentId("review-agent"),
        agent_run_id=REVIEW_RUN,
        title="Seeded review conversation",
        user_text="Inspect the implementation and its browser coverage.",
        assistant_text="I found no navigation regressions in the seeded workflow.",
        summary=review.summary,
    )


async def _seed_step_conversation(
    store: SQLiteStateStore,
    *,
    instance_id: AgentInstanceId,
    conversation_id: ConversationId,
    step_id: StepId,
    agent_id: AgentId,
    agent_run_id: AgentRunId,
    title: str,
    user_text: str,
    assistant_text: str,
    summary: str,
) -> None:
    await store.create_instance(
        agent_id,
        task_id=TASK_ID,
        runner="codex",
        instance_id=instance_id,
        conversation_id=conversation_id,
        workflow_run_id=RUN_ID,
        workflow_step_id=step_id,
    )
    await store.update_instance_metadata(
        instance_id,
        title=title,
        archived=False,
        runner="codex",
    )
    await store.append_messages(
        instance_id,
        (Message.user(user_text), Message.assistant(assistant_text)),
    )
    await store.record_agent_run(
        AgentRun(
            agent_run_id=agent_run_id,
            instance_id=instance_id,
            status=AgentRunStatus.SUCCEEDED,
            summary=summary,
            runner="codex",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    asyncio.run(seed(Path(args.state) / "conversations.sqlite3", args.repository))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
