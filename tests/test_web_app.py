"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace

import httpx
import openengine as oe
import pytest

import github_fakes
from engine.adapters.agent_runner.claude_code import ClaudeCodeAgentRunner
from engine.adapters.agent_runner.codex import (
    INTERACTIVE_APPROVAL_POLICY,
    CodexAgentRunner,
)
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import ApprovalFeed, ThreadService, create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_review_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalKind,
    ApprovalStatus,
    ConversationId,
    HumanReviewCompleted,
    Message,
    Role,
    RunFailed,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StepCompleted,
    StepId,
    StepReactivated,
    StepOutput,
    TaskId,
    ToolCall,
    WorkflowId,
    WorkspaceId,
    WorkspaceProvisioned,
)
from engine.ports import (
    AgentTurn,
    ApprovalRequest,
    InteractiveAgentRunner,
    McpServerConfig,
    UserInputAnswer,
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
    Workspace,
    WorkspaceState,
)
from engine.runtime import (
    BUILT_IN,
    INVALID_COMPLETION_ERROR,
    PLANNER,
    AgentSession,
    ApprovalBroker,
    ApprovalCapability,
    ApprovalConfig,
    Capabilities,
    EngineConfig,
    WorkflowCatalog,
)
from engine.runtime.terminal_mcp import _mcp_response
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR

CODER = AgentId("coder")
WORKFLOW_ID = WorkflowId("implementation-review-v1")
IMPLEMENTATION_STEP = StepId("implementation")
REVIEW_STEP = StepId("review")
HUMAN_REVIEW_STEP = StepId("human-review")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Be terse.",
        description="Reads code.",
    )
}


def test_web_composes_the_sqlite_conversation_store(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"

    capabilities = build_capabilities(Settings(sqlite_path=str(database)))

    assert isinstance(capabilities.state_store, SQLiteStateStore)
    assert database.exists()
    capabilities.state_store.close()


def test_web_offers_one_interactive_runner_per_cli() -> None:
    runners = build_runners(Settings())

    assert tuple(runners) == ("codex", "claude")
    assert isinstance(runners["codex"], CodexAgentRunner)
    assert isinstance(runners["claude"], ClaudeCodeAgentRunner)
    # Which of them pause is what decides whether a run brokers approvals, so
    # it is read off the port rather than off the class name.
    assert isinstance(runners["codex"], InteractiveAgentRunner)
    assert isinstance(runners["claude"], InteractiveAgentRunner)


def test_the_runner_nobody_is_watching_stays_read_only(tmp_path) -> None:
    """One class serves both callers now, so the sandbox is the whole difference.

    `build_runners` widens it because someone is there to approve; the port
    implementation a non-interactive caller reaches for has nobody to ask, and
    must not have been widened along with it.
    """
    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        argv = capabilities.agent_runner.command_line(PROFILES[CODER])
    finally:
        capabilities.state_store.close()

    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_interactive_runners_may_do_what_the_user_approves() -> None:
    """A gate is only a gate if what it lets through can then happen."""
    runners = build_runners(Settings())

    codex_argv = runners["codex"].command_line(PROFILES[CODER])
    claude_argv = runners["claude"].interactive_command_line(PROFILES[CODER])
    preapproved = claude_argv[
        claude_argv.index("--allowedTools") + 1 : claude_argv.index("--input-format")
    ]

    # Codex: writable inside the worktree, and stopping to ask before it would
    # step outside one.
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "workspace-write"
    assert INTERACTIVE_APPROVAL_POLICY == "on-request"
    # Claude: reads run unattended, everything else reaches the user.
    assert preapproved == ["Read", "Glob", "Grep"]
    assert "Bash" not in preapproved
    assert "Edit" not in preapproved
    assert claude_argv[claude_argv.index("--permission-prompt-tool") + 1] == "stdio"


def test_the_configured_policy_builds_the_interactive_claude_runner() -> None:
    """`engine.toml` is where chat's permissions are written down.

    A preapproved tool is one whose requests never reach the callback at all,
    which is the only thing a provider allow-list can express. Shell stays off
    it however granted: a shell rule is written per command, and the patterns
    live where the requests arrive.
    """
    granted = EngineConfig(
        approvals=ApprovalConfig(
            allow=(ApprovalCapability.READ, ApprovalCapability.EDIT, ApprovalCapability.BASH)
        )
    )
    argv = build_runners(Settings(engine_config=granted))["claude"].command_line(
        PROFILES[CODER]
    )

    preapproved = argv[argv.index("--allowedTools") + 1 :]
    assert preapproved == ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit"]
    assert "Bash" not in preapproved


def test_the_interactive_codex_sandbox_is_not_narrowed_by_the_policy() -> None:
    """A sandbox is a ceiling, not a preapproval.

    A capability absent from `allow` is one nobody has ruled on, so a person may
    still allow it mid-turn -- and a sandbox narrowed before the turn started
    would refuse the write they just approved. Codex's policy is applied to its
    requests instead.
    """
    reads_only = EngineConfig(approvals=ApprovalConfig(allow=(ApprovalCapability.READ,)))
    argv = build_runners(Settings(engine_config=reads_only))["codex"].command_line(
        PROFILES[CODER]
    )

    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_engine_config_disables_attribution_for_every_workflow_runner() -> None:
    runners = build_workflow_runners(Settings(engine_config=EngineConfig(attribution=False)))

    codex_argv = runners["codex"].command_line(PROFILES[CODER])
    claude_argv = runners["claude"].command_line(PROFILES[CODER])
    assert "developer_instructions=" in codex_argv[codex_argv.index("-c") + 1]
    assert json.loads(claude_argv[claude_argv.index("--settings") + 1])[
        "attribution"
    ]["commit"] == ""


def test_workflow_runners_are_write_enabled_only_inside_the_worktree() -> None:
    runners = build_workflow_runners(Settings())

    codex_argv = runners["codex"].command_line(PROFILES[CODER])
    claude_argv = runners["claude"].command_line(PROFILES[CODER])
    claude_tools = claude_argv[claude_argv.index("--allowedTools") + 1 :]

    assert codex_argv[codex_argv.index("--sandbox") + 1] == "workspace-write"
    assert claude_tools == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert "Bash" not in claude_tools


def test_every_workflow_runner_is_reviewed_by_a_read_only_runner_of_its_name() -> None:
    """What keeps a review read-only is the runner it gets, not its prompt."""
    settings = Settings()
    workflow_runners = build_workflow_runners(settings)
    reviewers = build_review_runners(settings)

    assert set(workflow_runners) <= set(reviewers)
    codex_argv = reviewers["codex"].command_line(PROFILES[CODER])
    claude_argv = reviewers["claude"].command_line(PROFILES[CODER])

    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"
    assert claude_argv[claude_argv.index("--allowedTools") + 1 :] == [
        "Read",
        "Glob",
        "Grep",
    ]


def test_a_planning_chat_is_answered_by_the_runner_that_cannot_write(tmp_path) -> None:
    """The Plan button's whole difference from New chat.

    Same provider the user picked, same conversation machinery -- and a set of
    tools that cannot change the checkout it is reading, which is a property of
    what the composition hands the planner rather than of its instructions.
    """
    settings = Settings(
        # The policy this repository ships: chat may edit what it is asked to.
        # A planning conversation is what that permission does not reach.
        engine_config=EngineConfig(
            approvals=ApprovalConfig(
                allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)
            )
        ),
        sqlite_path=str(tmp_path / "conversations.sqlite3"),
    )
    capabilities = build_capabilities(settings)
    try:
        session = build_session(
            capabilities,
            build_runners(settings),
            read_only_runners=build_review_runners(settings),
        )
        planner = session.runner_for(PLANNER.agent_id, "claude")
        coder = session.runner_for(CODER, "claude")
    finally:
        capabilities.state_store.close()

    planner_argv = planner.command_line(PLANNER)
    coder_argv = coder.command_line(PROFILES[CODER])

    assert planner_argv[planner_argv.index("--allowedTools") + 1 :] == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert "Edit" in coder_argv[coder_argv.index("--allowedTools") + 1 :]
    codex_argv = session.runner_for(PLANNER.agent_id, "codex").command_line(PLANNER)
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"


def test_review_comments_are_left_with_the_gh_the_composition_names(tmp_path) -> None:
    """Which `gh` runs is the composition's to say, as the two CLIs are.

    Proved by leaving a comment rather than by reading the constructor argument
    back, because what matters is the executable the adapter actually spawns:
    an unwired `github_binary` spawns whatever `gh` is on PATH, which for a
    reviewer means somebody's real repository.
    """
    log = tmp_path / "gh.jsonl"
    capabilities = build_capabilities(
        Settings(
            github_binary=github_fakes.install(tmp_path, log),
            sqlite_path=str(tmp_path / "c.sqlite3"),
        )
    )
    try:
        asyncio.run(
            capabilities.source_control.add_comment(
                "https://github.com/acme/api/pull/7", "Looks right."
            )
        )
    finally:
        capabilities.state_store.close()

    assert [call["argv"] for call in github_fakes.calls(log)] == [
        ["pr", "comment", "https://github.com/acme/api/pull/7", "--body", "Looks right."]
    ]


def test_web_restores_sqlite_conversations_after_restart(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"
    runner = ConcurrentRunner()
    other_runner = ConcurrentRunner(("persisted answer",))
    runners = {"test": runner, "other": other_runner}

    first_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    first_app = create_app(
        AgentSession(first_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def first_process() -> str:
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "remember this", "runner": "other"},
            )
            renamed = await client.patch(
                f"/api/threads/{thread_id}",
                json={"title": "Persistent metadata"},
            )
            archived = await client.post(f"/api/threads/{thread_id}/archive")
            assert renamed.status_code == 200
            assert archived.status_code == 200
            return thread_id

    thread_id = asyncio.run(first_process())
    first_capabilities.state_store.close()

    second_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    second_app = create_app(
        AgentSession(second_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def second_process():
        transport = httpx.ASGITransport(app=second_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            threads = await client.get("/api/threads")
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return threads, messages

    try:
        threads, messages = asyncio.run(second_process())
    finally:
        second_capabilities.state_store.close()

    assert threads.json()["threads"] == [
        {
            "id": thread_id,
            "title": "Persistent metadata",
            "archived": True,
            "agentId": "coder",
            "runner": "other",
            "workspaceAttached": False,
        }
    ]
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [("user", "remember this"), ("assistant", "persisted answer")]


class ConcurrentRunner:
    """A controllably slow runner that records how much work overlaps."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[Message, ...]] = []
        self.workspace_ids: list[str | None] = []
        self.active = 0
        self.most_active = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        reply = self.replies.pop(0) if self.replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


class PausingWorkflowRunner(ConcurrentRunner):
    """Pause a workflow without opening the terminal MCP test server."""

    def __init__(self) -> None:
        super().__init__()
        self.workflow_attempts = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        if str(agent_run_id).endswith(":name:run"):
            return AgentTurn(Message.assistant("Paused workflow"))
        self.workflow_attempts += 1
        self.seen.append(tuple(messages))
        return AgentTurn(
            Message.assistant(
                tool_calls=(
                    ToolCall("clarification", "request_user_input", "{}"),
                )
            )
        )


class BlockingWorkflowRunner(ConcurrentRunner):
    """Hold an agent dispatch open until the web service interrupts it."""

    def __init__(self) -> None:
        super().__init__()
        self.workflow_attempts = 0
        self.started = asyncio.Event()
        self.never = asyncio.Event()

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        if str(agent_run_id).endswith(":name:run"):
            return AgentTurn(Message.assistant("Blocked workflow"))
        self.workflow_attempts += 1
        self.seen.append(tuple(messages))
        self.started.set()
        await self.never.wait()
        raise AssertionError("the workflow turn should be interrupted")


def _rejected(acknowledgement: dict[str, object] | None) -> bool:
    """Whether the broker refused a terminal tool call instead of accepting it."""
    result = (acknowledgement or {}).get("result")
    return isinstance(result, dict) and result.get("isError") is True


class TerminalToolRunner(ConcurrentRunner):
    """Call one workflow terminal tool through the attached MCP server."""

    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, object],
        name: str = "Named workflow",
    ) -> None:
        super().__init__((name,))
        self.tool_name = tool_name
        self.arguments = arguments
        self.cancelled = asyncio.Event()
        self.acknowledgement: dict[str, object] | None = None

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        host = mcp_server.args[mcp_server.args.index("--host") + 1]
        port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
        token = mcp_server.args[mcp_server.args.index("--token") + 1]
        self.acknowledgement = await _mcp_response(
            host,
            port,
            token,
            {
                "jsonrpc": "2.0",
                "id": "workflow-tool-call-1",
                "method": "tools/call",
                "params": {
                    "name": self.tool_name,
                    "arguments": self.arguments,
                },
            },
        )
        if _rejected(self.acknowledgement):
            # A refused tool call leaves the CLI running, so it ends its turn
            # the way any provider would. Only an accepted result is followed
            # by the runtime cancelling the process.
            return AgentTurn(Message.assistant("The terminal call was refused."))
        if self.tool_name == "clarify":
            call = ToolCall(
                "workflow-tool-call-1",
                "mcp__workflow__clarify",
                json.dumps(self.arguments),
            )
            return AgentTurn(
                Message.assistant("The existing behavior is intentional."),
                steps=(Message.assistant(tool_calls=(call,)),),
            )
        await self.cancelled.wait()
        return AgentTurn(Message.assistant("Terminal result accepted."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        self.cancelled.set()


class WorkflowProgressRunner(TerminalToolRunner):
    """Hold a streamed MCP workflow turn open so its progress can be observed."""

    def __init__(self) -> None:
        super().__init__(
            "complete_step",
            {
                "outcome": "success",
                "summary": "Progress streamed.",
                "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
            },
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn_with_mcp_streamed(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_message,
        workspace_id=None,
    ) -> AgentTurn:
        progress = Message.assistant("Inspecting the implementation.")
        on_message(progress)
        self.started.set()
        await self.release.wait()
        turn = await super().run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )
        on_message(turn.message)
        return AgentTurn(turn.message, steps=(progress, *turn.steps))


class InterruptibleImplementationRunner(TerminalToolRunner):
    """Wait on the first turn, then complete after a human follow-up."""

    def __init__(self) -> None:
        super().__init__(
            "complete_step",
            {
                "outcome": "success",
                "summary": "Applied the human guidance.",
                "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
            },
        )
        self.started = asyncio.Event()
        self.never = asyncio.Event()
        self.attempts = 0
        self.cancel_calls = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.attempts += 1
        if self.attempts == 1:
            self.seen.append(tuple(messages))
            self.workspace_ids.append(workspace_id)
            self.started.set()
            await self.never.wait()
            raise AssertionError("the first implementation turn should be interrupted")
        return await super().run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        self.cancel_calls += 1
        if self.attempts > 1:
            self.cancelled.set()


class ApprovalWorkflowRunner(TerminalToolRunner):
    """Pause an MCP workflow turn until its conversation approves a command."""

    def __init__(self) -> None:
        super().__init__(
            "complete_step",
            {
                "outcome": "success",
                "summary": "Approved work completed.",
                "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
            },
        )
        self.decisions: list[ApprovalDecision] = []

    async def run_turn_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        decision = await on_approval(self._request())
        self.decisions.append(decision)
        return AgentTurn(Message.assistant("Approval answered."))

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_approval,
        on_message=None,
        workspace_id=None,
    ) -> AgentTurn:
        decision = await on_approval(self._request())
        self.decisions.append(decision)
        if decision is ApprovalDecision.CANCEL:
            return AgentTurn(Message.assistant("The command was cancelled."))
        return await super().run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )

    @staticmethod
    def _request() -> ApprovalRequest:
        return ApprovalRequest(
            approval_id="provider-workflow-approval",
            kind=ApprovalKind.COMMAND_EXECUTION,
            reason="Run the workflow test suite",
            command="pytest",
            cwd="/workspace",
        )


class RepeatedApprovalWorkflowRunner(ApprovalWorkflowRunner):
    """Raise another request after the first one is approved."""

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_approval,
        on_message=None,
        workspace_id=None,
    ) -> AgentTurn:
        for command in ("pytest", "ruff check"):
            decision = await on_approval(replace(self._request(), command=command))
            self.decisions.append(decision)
            if decision is ApprovalDecision.CANCEL:
                return AgentTurn(Message.assistant("The command was cancelled."))
        return await TerminalToolRunner.run_turn_with_mcp(
            self, agent_run_id, profile, messages, mcp_server, workspace_id
        )


class QuestionWorkflowRunner(ApprovalWorkflowRunner):
    """Pause an MCP workflow turn for structured human input."""

    def __init__(self) -> None:
        super().__init__()
        self.response: UserInputResponse | None = None

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_approval,
        on_message=None,
        workspace_id=None,
    ) -> AgentTurn:
        response = await on_approval(
            ApprovalRequest(
                approval_id="provider-question",
                kind=ApprovalKind.USER_INPUT,
                tool_name="AskUserQuestion",
                allowed_decisions=(ApprovalDecision.CANCEL,),
                questions=(UserInputQuestion(
                    question_id="api",
                    header="API",
                    question="Which API should remain stable?",
                    options=(
                        UserInputOption("Public", "Preserve the public API"),
                        UserInputOption("Internal", "Preserve the internal API"),
                    ),
                ),),
                requires_human=True,
            )
        )
        assert isinstance(response, UserInputResponse)
        self.response = response
        return await TerminalToolRunner.run_turn_with_mcp(
            self, agent_run_id, profile, messages, mcp_server, workspace_id
        )


class InvalidThenTerminalToolRunner(TerminalToolRunner):
    """Exit once without a valid call, then complete through MCP."""

    def __init__(self, arguments: dict[str, object]) -> None:
        super().__init__("complete_step", arguments)
        self.attempts = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.attempts += 1
        if self.attempts == 1:
            self.seen.append(tuple(messages))
            self.workspace_ids.append(workspace_id)
            return AgentTurn(
                Message.assistant(
                    '{"outcome":"success","summary":"Legacy response","outputs":{}}'
                )
            )
        return await super().run_turn_with_mcp(
            agent_run_id,
            profile,
            messages,
            mcp_server,
            workspace_id,
        )


class ClarificationToolRunner(ConcurrentRunner):
    """Request user input instead of completing the bound step."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.attempts += 1
        question = ToolCall(
            "question-1",
            "AskUserQuestion",
            json.dumps({"question": "Which behavior should remain compatible?"}),
        )
        return AgentTurn(
            Message.assistant("Waiting for clarification."),
            steps=(Message.assistant(tool_calls=(question,)),),
        )


def _session(runner: ConcurrentRunner) -> AgentSession:
    return _session_with({"test": runner})


def _session_with(
    runners: Mapping[str, ConcurrentRunner],
    profiles: Mapping[AgentId, AgentProfile] = PROFILES,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=next(iter(runners.values())),
            communications=unused,
            workspace_provider=unused,
            state_store=InMemoryStateStore(),
        ),
        profiles=profiles,
        runners=dict(runners),
    )


def _workflow_state(
    phase: RunPhase, active_step: StepId = IMPLEMENTATION_STEP
) -> RunState:
    run_id = RunId(f"run-{phase.value}-{active_step}")
    implementation = StepCompleted(
        run_id=run_id,
        step_id=IMPLEMENTATION_STEP,
        agent_run_id=AgentRunId("implementation-execution"),
        outcome="success",
        summary="Implemented the lock and regression test.",
        outputs=(StepOutput("pr_url", "https://github.com/acme/api/pull/42"),),
    )
    review = StepCompleted(
        run_id=run_id,
        step_id=REVIEW_STEP,
        agent_run_id=AgentRunId("review-execution"),
        outcome="changes_requested",
        summary="The test should also cover cancellation.",
        outputs=(StepOutput("findings", "Cancellation coverage missing"),),
    )
    values = {
        RunPhase.AWAITING_HUMAN_REVIEW: (
            HUMAN_REVIEW_STEP,
            (implementation, review),
            None,
            "",
        ),
        RunPhase.SUCCEEDED: (
            HUMAN_REVIEW_STEP,
            (implementation, review),
            HumanReviewCompleted(
                run_id=run_id,
                step_id=HUMAN_REVIEW_STEP,
                approved=True,
                summary="The residual risk is acceptable.",
            ),
            "",
        ),
        RunPhase.FAILED: (
            IMPLEMENTATION_STEP,
            (
                StepCompleted(
                    run_id=run_id,
                    step_id=IMPLEMENTATION_STEP,
                    agent_run_id=AgentRunId("implementation-execution"),
                    outcome="failed",
                    summary="Tests did not pass.",
                ),
            ),
            None,
            "Tests did not pass.",
        ),
    }
    if phase is RunPhase.RUNNING_AGENT:
        current_step, results, decision, reason = (
            (REVIEW_STEP, (implementation,), None, "")
            if active_step == REVIEW_STEP
            else (IMPLEMENTATION_STEP, (), None, "")
        )
    else:
        current_step, results, decision, reason = values[phase]
    return RunState(
        run_id=run_id,
        task_id=TaskId("task-42"),
        workflow_id=WORKFLOW_ID,
        phase=phase,
        repository="acme/api",
        prompt="Fix the race and add a regression test.",
        current_step_id=current_step,
        current_agent_run_id=(
            AgentRunId(f"current-{current_step}")
            if current_step != HUMAN_REVIEW_STEP
            else None
        ),
        step_results=results,
        human_review=decision,
        failure_reason=reason,
    )


def _reviewer(
    summary: str = "The change matches the task.",
    findings: str = "No blocking findings.",
) -> TerminalToolRunner:
    """A reviewer that completes its step with the output the step declares."""
    return TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": summary,
            "outputs": {"findings": findings},
        },
    )


def _workflow_app(
    store: InMemoryStateStore,
    runner: ConcurrentRunner,
    workspaces: object | None = None,
    workflow_runners: dict[str, ConcurrentRunner] | None = None,
    reviewers: dict[str, ConcurrentRunner] | None = None,
    workflow_catalog: WorkflowCatalog | None = None,
    workspace_repository: str | None = None,
):
    """Wire the app the way the composition root does.

    `workflow_runners` implement and may write; the chat runner registered under
    the same name reviews, and is the one that may not.
    """
    unused = object()
    implementers = dict(workflow_runners or {"test": runner})
    chat_runners: dict[str, ConcurrentRunner] = dict(
        reviewers or {name: _reviewer() for name in implementers}
    )
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces or ConversationWorkspaces(),
            state_store=store,
        ),
        profiles=PROFILES,
        runners=chat_runners,
        workspace_repository=workspace_repository,
    )
    return create_app(
        session,
        chat_runners,
        workflow_runners=implementers,
        review_runners=chat_runners,
        workflow_catalog=workflow_catalog,
    )


def _human_then_agent_catalog() -> WorkflowCatalog:
    worker = oe.agent(id="follow-up-agent", instructions="Continue after approval.")
    definition = oe.workflow(
        id="human-then-agent-v1",
        name="Human then agent",
        version="v1",
        steps=[
            oe.human_review_step(
                id="approval",
                name="Approval",
                title=oe.template("Approve follow-up"),
                summary=oe.template("Choose whether to continue"),
                approved=oe.goto("follow-up"),
                rejected=oe.fail(),
            ),
            oe.agent_step(
                id="follow-up",
                name="Follow-up",
                agent=worker,
                prompt=oe.template("Continue the task"),
                editable=True,
                workspace_access="write",
                transitions={"*": oe.succeed()},
            ),
        ],
    )
    return WorkflowCatalog.from_definitions((definition,))


async def _await_phase(
    client: httpx.AsyncClient, run_id: RunId, phase: str
) -> httpx.Response:
    """Poll a run until it reaches `phase`, or return the last view it had."""
    for _ in range(200):
        response = await client.get(f"/api/runs/{run_id}")
        if response.json()["phase"] == phase:
            return response
        await asyncio.sleep(0.01)
    return response


@pytest.mark.parametrize(
    ("phase", "active_step", "terminal_outcome"),
    [
        (RunPhase.RUNNING_AGENT, IMPLEMENTATION_STEP, None),
        (RunPhase.RUNNING_AGENT, REVIEW_STEP, None),
        (RunPhase.AWAITING_HUMAN_REVIEW, HUMAN_REVIEW_STEP, None),
        (RunPhase.SUCCEEDED, HUMAN_REVIEW_STEP, "approved"),
        (RunPhase.FAILED, IMPLEMENTATION_STEP, "failed"),
    ],
)
def test_run_api_covers_workflow_lifecycle_phases(
    phase: RunPhase, active_step: StepId, terminal_outcome: str | None
) -> None:
    store = InMemoryStateStore()
    state = _workflow_state(phase, active_step)
    asyncio.run(store.save(state))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/runs")
            detail = await client.get(f"/api/runs/{state.run_id}")
            return listed, detail

    listed, detail = asyncio.run(scenario())
    body = detail.json()

    assert listed.status_code == 200
    assert [run["runId"] for run in listed.json()["runs"]] == [state.run_id]
    assert detail.status_code == 200
    assert body["taskPrompt"] == "Fix the race and add a regression test."
    assert body["name"] == body["taskPrompt"]
    assert body["workflowId"] == "implementation-review-v1"
    assert body["phase"] == phase.value
    assert body["currentStepId"] == state.current_step_id
    assert body["terminalOutcome"] == terminal_outcome
    assert [step["stepId"] for step in body["steps"]] == [
        IMPLEMENTATION_STEP,
        REVIEW_STEP,
        HUMAN_REVIEW_STEP,
    ]

    if phase is RunPhase.AWAITING_HUMAN_REVIEW:
        assert body["pendingHumanReview"] is not None
        assert "Implemented the lock" in body["pendingHumanReview"]["summary"]
        assert body["steps"][1]["changesRequested"] is True
        assert body["humanDecision"] is None
    if phase is RunPhase.SUCCEEDED:
        assert body["humanDecision"] == {
            "stepId": "human-review",
            "approved": True,
            "outcome": "approved",
            "summary": "The residual risk is acceptable.",
        }
        assert body["steps"][1]["outcome"] == "changes_requested"


def test_create_workflow_run_implements_reviews_and_awaits_a_human() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Added cancellation handling.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    reviewer = _reviewer(
        summary="The handling is correct.",
        findings="worker.py cancels the task, and the new test covers it.",
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Add cancellation handling.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            listed = await client.get("/api/runs")
            instances = await store.list_instances(workflow_run_id=run_id)
            conversations = {
                instance.workflow_step_id: await store.load_conversation(
                    instance.instance_id
                )
                for instance in instances
            }
            return created, reopened, listed, await store.history(run_id), conversations

    created, reopened, listed, history, conversations = asyncio.run(scenario())
    body = reopened.json()

    assert created.status_code == 201
    assert created.json()["phase"] == "pending"
    assert created.json()["currentStepId"] is None
    assert body["phase"] == "awaiting_human_review"
    assert body["currentStepId"] == "human-review"
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][0]["summary"] == "Added cancellation handling."
    assert body["steps"][0]["conversationUrl"]
    assert body["steps"][1]["status"] == "completed"
    assert body["steps"][1]["outcome"] == "success"
    assert body["steps"][1]["summary"] == "The handling is correct."
    assert body["steps"][1]["outputs"] == [
        {"name": "findings", "value": "worker.py cancels the task, and the new test covers it."}
    ]
    assert body["steps"][1]["conversationUrl"]
    assert body["steps"][2]["status"] == "action_required"
    assert "worker.py cancels the task" in body["pendingHumanReview"]["summary"]
    assert [run["runId"] for run in listed.json()["runs"]] == [
        created.json()["runId"]
    ]
    assert len(history) == 5
    assert isinstance(history[0], RunRequested)
    assert isinstance(history[1], WorkspaceProvisioned)
    assert isinstance(history[2], RunNamed)
    assert isinstance(history[3], StepCompleted)
    assert isinstance(history[4], StepCompleted)
    assert history[0].prompt == "Add cancellation handling."
    assert history[0].repository == "acme/api"
    assert (history[3].step_id, history[4].step_id) == (
        IMPLEMENTATION_STEP,
        REVIEW_STEP,
    )
    # Two executions, one each, in the single workspace the run provisioned.
    assert implementer.workspace_ids == ["ws-1", "ws-1"]
    assert reviewer.workspace_ids == ["ws-1"]
    # Two conversations, kept apart: the review is its own durable instance.
    assert set(conversations) == {IMPLEMENTATION_STEP, REVIEW_STEP}
    assert all(conversation.messages for conversation in conversations.values())
    assert "Name this workflow" in implementer.seen[0][-1].content
    assert "`complete_step`" in implementer.seen[1][0].content
    assert "JSON" not in implementer.seen[1][0].content


def test_startup_restarts_a_review_whose_command_was_lost() -> None:
    store = InMemoryStateStore()
    state = _workflow_state(RunPhase.RUNNING_AGENT, REVIEW_STEP)
    state = replace(
        state,
        workspace_id=WorkspaceId("ws-1"),
        current_agent_run_id=AgentRunId(f"{state.run_id}:review:run"),
    )
    reviewer = ConcurrentRunner()
    app = _workflow_app(
        store,
        ConcurrentRunner(),
        workflow_runners={
            "other": ConcurrentRunner(),
            "test": ConcurrentRunner(),
        },
        reviewers={"other": ConcurrentRunner(), "test": reviewer},
    )

    async def scenario():
        await store.save(state)
        await store.create_instance(
            AgentId("implementation-agent"),
            workspace_id=state.workspace_id,
            instance_id=AgentInstanceId(f"{state.run_id}:implementation:instance"),
            conversation_id=ConversationId(
                f"{state.run_id}:implementation:instance:conversation"
            ),
            workflow_run_id=state.run_id,
            workflow_step_id=IMPLEMENTATION_STEP,
            runner="test",
        )
        async with app.router.lifespan_context(app):
            for _ in range(200):
                instances = await store.list_instances(workflow_run_id=state.run_id)
                if any(
                    instance.workflow_step_id == REVIEW_STEP
                    for instance in instances
                ):
                    return instances
                await asyncio.sleep(0.01)
            return instances

    instances = asyncio.run(scenario())

    assert {instance.workflow_step_id for instance in instances} == {
        IMPLEMENTATION_STEP,
        REVIEW_STEP,
    }
    assert next(
        instance.runner
        for instance in instances
        if instance.workflow_step_id == REVIEW_STEP
    ) == "test"


def test_the_reviewer_reads_the_task_and_the_implementation_result() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Took the lock around the shared counter.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    reviewer = _reviewer()
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Fix the race and add a regression test.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            return await _await_phase(client, run_id, "awaiting_human_review")

    reopened = asyncio.run(scenario())
    prompt = reviewer.seen[0][0].content

    assert reopened.json()["phase"] == "awaiting_human_review"
    assert "Fix the race and add a regression test." in prompt
    assert "Took the lock around the shared counter." in prompt
    assert "Outcome: success" in prompt
    assert "do not modify" in prompt
    # The step declares an output, so the reviewer is told to report one.
    assert "findings" in prompt
    # The write-enabled runner named and implemented, but did not review.
    assert len(implementer.seen) == 2
    assert len(reviewer.seen) == 1


def test_implementation_conversation_periodically_streams_durable_progress() -> None:
    store = InMemoryStateStore()
    runner = WorkflowProgressRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Show implementation progress.",
                    "repository": "acme/api",
                },
            )
            run_id = created.json()["runId"]
            for _ in range(50):
                if runner.started.is_set():
                    break
                current = await client.get(f"/api/runs/{run_id}")
                assert current.json()["phase"] != "failed", current.json()[
                    "failureReason"
                ]
                await asyncio.sleep(0.01)
            assert runner.started.is_set()
            detail = await client.get(f"/api/runs/{run_id}")
            instance_id = detail.json()["steps"][0]["agentInstanceId"]

            for _ in range(20):
                conversation = await store.load_conversation(instance_id)
                if conversation is not None and len(conversation.messages) >= 2:
                    break
                await asyncio.sleep(0.01)

            loaded = await client.get(f"/api/threads/{instance_id}/messages")
            streaming = asyncio.create_task(
                client.get(f"/api/threads/{instance_id}/runs/current")
            )
            await asyncio.sleep(0.05)
            runner.release.set()
            response = await asyncio.wait_for(streaming, timeout=2)
            finished = await store.load_conversation(instance_id)
            events = [json.loads(line) for line in response.text.splitlines()]
            return loaded.json(), events, finished

    loaded, events, finished = asyncio.run(scenario())

    assert loaded["unstable_resume"] is True
    assert [message["role"] for message in loaded["messages"]] == ["user"]
    assert events[0] == {
        "type": "content",
        "content": [{"type": "text", "text": "Inspecting the implementation."}],
    }
    assert events[-1] == {
        "type": "done",
        "content": [
            {"type": "text", "text": "Inspecting the implementation."},
            {"type": "text", "text": "Terminal result accepted."},
        ],
    }
    assert finished is not None
    assert [message.role for message in finished.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.ASSISTANT,
    ]
    assert [message.content for message in finished.messages[1:]] == [
        "Inspecting the implementation.",
        "Terminal result accepted.",
    ]


def test_editable_implementation_conversation_can_interrupt_and_continue() -> None:
    store = InMemoryStateStore()
    runner = InterruptibleImplementationRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement the configurable behavior.",
                    "repository": "acme/api",
                },
            )
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            run_id = created.json()["runId"]
            detail = await client.get(f"/api/runs/{run_id}")
            implementation_id = detail.json()["steps"][0]["agentInstanceId"]
            implementation = await client.get(f"/api/threads/{implementation_id}")

            stopped = await client.delete(
                f"/api/threads/{implementation_id}/runs/current"
            )
            still_implementing = await client.get(f"/api/runs/{run_id}")
            continued = await client.post(
                f"/api/threads/{implementation_id}/runs",
                json={"text": "Keep the public response shape unchanged."},
            )
            completed = await _await_phase(client, RunId(run_id), "awaiting_human_review")
            review_id = completed.json()["steps"][1]["agentInstanceId"]
            review = await client.get(f"/api/threads/{review_id}")
            refused = await client.post(
                f"/api/threads/{review_id}/runs",
                json={"text": "Change the review."},
            )
            conversation = await store.load_conversation(implementation_id)
            return (
                implementation,
                stopped,
                still_implementing,
                continued,
                completed,
                review,
                refused,
                conversation,
            )

    (
        implementation,
        stopped,
        still_implementing,
        continued,
        completed,
        review,
        refused,
        conversation,
    ) = asyncio.run(scenario())

    assert implementation.json()["editable"] is True
    assert stopped.status_code == 204
    assert still_implementing.json()["phase"] == "running_agent"
    assert continued.status_code == 200
    assert completed.json()["steps"][0]["summary"] == "Applied the human guidance."
    assert review.json()["editable"] is False
    assert refused.status_code == 409
    assert "read-only" in refused.json()["error"]
    assert runner.cancel_calls >= 2  # interruption, then terminal MCP completion
    assert conversation is not None
    assert [
        message.content for message in conversation.messages if message.role is Role.USER
    ][-1] == "Keep the public response shape unchanged."
    assert [(message.role, message.content) for message in runner.seen[-1]] == [
        (Role.USER, conversation.messages[0].content),
        (Role.USER, "Keep the public response shape unchanged."),
    ]


def test_message_reactivates_a_closed_implementation_step() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Initial implementation.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    reviewer = _reviewer()
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement the configurable behavior.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            awaiting = await _await_phase(client, run_id, "awaiting_human_review")
            implementation_id = awaiting.json()["steps"][0]["agentInstanceId"]
            approved = await client.post(
                f"/api/runs/{run_id}/human-review",
                json={"approved": True, "summary": "Approved."},
            )

            implementer.arguments["summary"] = "Implementation updated from guidance."
            continued = await client.post(
                f"/api/threads/{implementation_id}/runs",
                json={"text": "Also preserve the legacy response header."},
            )
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            conversation = await store.load_conversation(implementation_id)
            return approved, continued, reopened, conversation, await store.history(run_id)

    approved, continued, reopened, conversation, history = asyncio.run(scenario())

    assert approved.json()["phase"] == "succeeded"
    assert continued.status_code == 200
    assert reopened.json()["phase"] == "awaiting_human_review"
    assert reopened.json()["steps"][0]["summary"] == (
        "Implementation updated from guidance."
    )
    assert reopened.json()["humanDecision"] is None
    assert sum(isinstance(event, StepReactivated) for event in history) == 1
    assert conversation is not None
    assert [
        message.content for message in conversation.messages if message.role is Role.USER
    ][-1] == "Also preserve the legacy response header."
    # The repeated review receives the new implementation result, rather than
    # silently replaying only its original, now-stale context.
    assert "Implementation updated from guidance." in reviewer.seen[-1][-1].content


def test_clarifying_a_closed_implementation_does_not_reactivate_the_run() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Initial implementation.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    app = _workflow_app(store, implementer, reviewers={"test": _reviewer()})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement the configurable behavior.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            awaiting = await _await_phase(client, run_id, "awaiting_human_review")
            implementation_id = awaiting.json()["steps"][0]["agentInstanceId"]
            approved = await client.post(
                f"/api/runs/{run_id}/human-review",
                json={"approved": True, "summary": "Approved."},
            )
            before = await store.load(run_id)
            history_before = await store.history(run_id)

            implementer.tool_name = "clarify"
            implementer.arguments = {}
            answered = await client.post(
                f"/api/threads/{implementation_id}/runs",
                json={"text": "Why does the legacy response header remain?"},
            )
            after = await store.load(run_id)
            history_after = await store.history(run_id)
            conversation = await store.load_conversation(implementation_id)
            return (
                approved,
                answered,
                before,
                after,
                history_before,
                history_after,
                conversation,
            )

    approved, answered, before, after, history_before, history_after, conversation = (
        asyncio.run(scenario())
    )

    assert approved.json()["phase"] == "succeeded"
    assert answered.status_code == 200
    assert after == before
    assert history_after == history_before
    assert not any(isinstance(event, StepReactivated) for event in history_after)
    assert conversation is not None
    assert conversation.messages[-1].content == "The existing behavior is intentional."


def test_approval_requests_pop_in_on_workflow_conversations() -> None:
    store = InMemoryStateStore()
    runner = ApprovalWorkflowRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Make the approved change.",
                    "repository": "acme/api",
                },
            )
            for _ in range(100):
                pending = await store.list_approvals(status=ApprovalStatus.PENDING)
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending
            request = pending[0]
            detail = await client.get(f"/api/runs/{created.json()['runId']}")

            streaming = asyncio.create_task(
                client.get(
                    f"/api/threads/{request.instance_id}/runs/current"
                )
            )
            await asyncio.sleep(0.05)
            decided = await client.post(
                f"/api/threads/{request.instance_id}/runs/current/approvals/"
                f"{request.approval_id}",
                json={"decision": "accept"},
            )
            response = await asyncio.wait_for(streaming, timeout=2)
            return request, detail, decided, [
                json.loads(line) for line in response.text.splitlines()
            ]

    request, detail, decided, events = asyncio.run(scenario())
    approvals = [event["approval"] for event in events if event["type"] == "approval"]

    assert approvals[0] == {
        "id": str(request.approval_id),
        "status": "pending",
        "kind": "command_execution",
        "reason": "Run the workflow test suite",
        "command": "pytest",
        "cwd": "/workspace",
        "toolName": None,
        "toolCallId": None,
        "arguments": None,
        "allowedDecisions": ["accept", "accept_for_session", "cancel"],
        "decision": None,
        "decisionSource": None,
    }
    assert detail.json()["steps"][0]["waiting"] is True
    assert decided.status_code == 200
    assert decided.json()["approval"]["decision"] == "accept"
    assert runner.decisions == [ApprovalDecision.ACCEPT]
    assert events[-1]["type"] == "done"


def test_approval_feed_replays_and_pushes_broker_transitions() -> None:
    store = InMemoryStateStore()
    feed = ApprovalFeed(store)
    broker = ApprovalBroker(store, observe=feed.publish)

    async def scenario():
        instance = await store.create_instance(CODER)
        stream = feed.stream(instance.instance_id)
        assert await anext(stream) == b": connected\n\n"

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-feed"),
            instance_id=instance.instance_id,
            runner="test",
            present=lambda _approval: asyncio.sleep(0),
        )
        waiting = asyncio.create_task(handler(ApprovalWorkflowRunner._request()))
        pending = await asyncio.wait_for(anext(stream), timeout=1)
        record = (await store.list_approvals())[0]
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-feed"),
        )
        decided = await asyncio.wait_for(anext(stream), timeout=1)
        await waiting
        await stream.aclose()
        return [
            json.loads(frame.decode().removeprefix("data:"))
            for frame in (pending, decided)
        ]

    events = asyncio.run(scenario())

    assert [event["status"] for event in events] == ["pending", "decided"]
    assert events[0]["id"] == events[1]["id"]
    assert events[1]["decision"] == "accept"


def test_workflow_conversation_replays_every_approval_after_reconnect() -> None:
    store = InMemoryStateStore()
    runner = RepeatedApprovalWorkflowRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Make two approved changes.",
                    "repository": "acme/api",
                },
            )
            for _ in range(100):
                approvals = await store.list_approvals()
                if approvals:
                    break
                await asyncio.sleep(0.01)
            assert approvals
            first = approvals[0]
            await client.post(
                f"/api/threads/{first.instance_id}/runs/current/approvals/"
                f"{first.approval_id}",
                json={"decision": "accept"},
            )
            for _ in range(100):
                approvals = await store.list_approvals()
                if len(approvals) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(approvals) == 2
            second = approvals[1]

            streaming = asyncio.create_task(
                client.get(f"/api/threads/{second.instance_id}/runs/current")
            )
            await asyncio.sleep(0.05)
            await client.post(
                f"/api/threads/{second.instance_id}/runs/current/approvals/"
                f"{second.approval_id}",
                json={"decision": "accept"},
            )
            response = await asyncio.wait_for(streaming, timeout=2)
            return approvals, [
                json.loads(line) for line in response.text.splitlines()
            ]

    approvals, events = asyncio.run(scenario())
    streamed = [event["approval"] for event in events if event["type"] == "approval"]

    assert [approval["id"] for approval in streamed[:2]] == [
        str(approvals[0].approval_id),
        str(approvals[1].approval_id),
    ]
    assert streamed[0]["status"] == "decided"
    assert streamed[1]["status"] == "pending"


def test_workflow_question_choices_resume_the_same_model_turn() -> None:
    store = InMemoryStateStore()
    runner = QuestionWorkflowRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Ask which API to preserve.",
                    "repository": "acme/api",
                },
            )
            for _ in range(100):
                pending = await store.list_approvals(status=ApprovalStatus.PENDING)
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending
            request = pending[0]
            answered = await client.post(
                f"/api/threads/{request.instance_id}/runs/current/approvals/"
                f"{request.approval_id}",
                json={"answers": {"api": ["Public"]}},
            )
            completed = await _await_phase(
                client, RunId(created.json()["runId"]), "awaiting_human_review"
            )
            return request, answered, completed

    request, answered, completed = asyncio.run(scenario())

    assert request.kind is ApprovalKind.USER_INPUT
    assert answered.status_code == 200
    assert answered.json()["approval"]["answers"] == {"api": ["Public"]}
    assert completed.status_code == 200
    # The response goes back through the provider callback without ending and
    # restarting the turn.
    assert runner.response == UserInputResponse((UserInputAnswer("api", ("Public",)),))


def test_implementation_conversation_can_enable_system_auto_approvals() -> None:
    store = InMemoryStateStore()
    runner = RepeatedApprovalWorkflowRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Make two automatically approved changes.",
                    "repository": "acme/api",
                },
            )
            for _ in range(100):
                pending = await store.list_approvals(status=ApprovalStatus.PENDING)
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending
            instance_id = pending[0].instance_id

            enabled = await client.patch(
                f"/api/threads/{instance_id}", json={"autoApprove": True}
            )
            completed = await _await_phase(
                client, RunId(created.json()["runId"]), "awaiting_human_review"
            )
            return enabled, completed, await store.load_instance(instance_id), (
                await store.list_approvals(instance_id=instance_id)
            )

    enabled, completed, instance, approvals = asyncio.run(scenario())

    assert enabled.status_code == 200
    assert enabled.json()["autoApprove"] is True
    assert completed.status_code == 200
    assert instance is not None and instance.auto_approve is True
    assert runner.decisions == [ApprovalDecision.ACCEPT, ApprovalDecision.ACCEPT]
    assert len(approvals) == 2
    assert {approval.decision_source for approval in approvals} == {
        ApprovalDecisionSource.POLICY
    }


def test_review_conversation_can_enable_system_auto_approvals() -> None:
    store = InMemoryStateStore()
    instance = asyncio.run(
        store.create_instance(
            AgentId("review-agent"),
            runner="test",
            workflow_run_id=RunId("run-review-auto-approve"),
            workflow_step_id=REVIEW_STEP,
        )
    )
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            before = await client.get(f"/api/threads/{instance.instance_id}")
            enabled = await client.patch(
                f"/api/threads/{instance.instance_id}", json={"autoApprove": True}
            )
            return before, enabled, await store.load_instance(instance.instance_id)

    before, enabled, stored = asyncio.run(scenario())

    assert before.json()["editable"] is False
    assert before.json()["autoApprove"] is False
    assert enabled.status_code == 200
    assert enabled.json()["editable"] is False
    assert enabled.json()["autoApprove"] is True
    assert stored is not None and stored.auto_approve is True


def test_conversation_transcript_carries_approvals_after_its_run_ends() -> None:
    """Reloading a finished step still shows what it was asked to allow.

    The run stream replays approvals, but it is only opened for a run this
    process is still executing. A step whose task has since finished has no
    stream to reconnect to, so its transcript has to carry them instead.
    """
    store = InMemoryStateStore()
    runner = ApprovalWorkflowRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Make the approved change.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(100):
                pending = await store.list_approvals(status=ApprovalStatus.PENDING)
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending
            request = pending[0]
            await client.post(
                f"/api/threads/{request.instance_id}/runs/current/approvals/"
                f"{request.approval_id}",
                json={"decision": "accept"},
            )
            await _await_phase(client, run_id, "awaiting_human_review")
            return request, await client.get(
                f"/api/threads/{request.instance_id}/messages"
            )

    request, loaded = asyncio.run(scenario())
    history = loaded.json()

    # Nothing is executing this run any more, so nothing will reopen the
    # stream: the transcript is the only thing left to carry the request.
    assert history["unstable_resume"] is False
    assert [approval["id"] for approval in history["approvals"]] == [
        str(request.approval_id)
    ]
    assert history["approvals"][0]["status"] == "decided"
    assert history["approvals"][0]["decision"] == "accept"
    assert history["approvals"][0]["command"] == "pytest"


def test_complete_step_mcp_call_completes_the_active_workflow_step() -> None:
    store = InMemoryStateStore()
    runner = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Completed through MCP.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Complete the implementation through MCP.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "awaiting_human_review"
    completed_step = reopened.json()["steps"][0]
    assert completed_step["status"] == "completed"
    assert completed_step["outcome"] == "success"
    assert completed_step["summary"] == "Completed through MCP."
    assert completed_step["outputs"] == [
        {"name": "pr_url", "value": "https://github.com/acme/api/pull/42"}
    ]
    assert completed_step["mcpRequestId"] == "workflow-tool-call-1"
    implementation = history[3]
    assert isinstance(implementation, StepCompleted)
    assert implementation.step_id == IMPLEMENTATION_STEP
    assert implementation.mcp_request_id == "workflow-tool-call-1"
    assert runner.cancelled.is_set()
    assert runner.acknowledgement == {
        "jsonrpc": "2.0",
        "id": "workflow-tool-call-1",
        "result": {
            "content": [{"type": "text", "text": "accepted"}],
            "structuredContent": {"accepted": True},
        },
    }


def test_invalid_exit_is_retried_and_then_completes_the_active_step() -> None:
    store = InMemoryStateStore()
    runner = InvalidThenTerminalToolRunner(
        {
            "outcome": "success",
            "summary": "Completed after correction.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        }
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Complete after an invalid exit.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            return await _await_phase(client, run_id, "awaiting_human_review")

    reopened = asyncio.run(scenario())

    assert reopened.json()["phase"] == "awaiting_human_review"
    assert reopened.json()["steps"][0]["summary"] == "Completed after correction."
    assert runner.attempts == 2
    assert runner.seen[2][-1] == Message.user(INVALID_COMPLETION_ERROR)


def test_clarification_call_leaves_the_active_step_implementing() -> None:
    store = InMemoryStateStore()
    runner = ClarificationToolRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Ask when a requirement is ambiguous.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(20):
                state = await store.load(run_id)
                if state is not None and state.current_agent_run_id is not None:
                    agent_run = await store.agent_run(state.current_agent_run_id)
                    if (
                        agent_run is not None
                        and agent_run.status is AgentRunStatus.SUCCEEDED
                    ):
                        break
                await asyncio.sleep(0.01)
            return await client.get(f"/api/runs/{run_id}"), await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "running_agent"
    assert reopened.json()["currentStepId"] == "implementation"
    assert reopened.json()["steps"][0]["waiting"] is True
    assert runner.attempts == 1
    assert not any(isinstance(event, (StepCompleted, RunFailed)) for event in history)


def test_clarification_pause_is_not_restarted_after_process_restart() -> None:
    store = InMemoryStateStore()
    runner = PausingWorkflowRunner()
    first_app = _workflow_app(store, runner)

    async def scenario() -> tuple[RunState, RunState]:
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Pause for clarification.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(100):
                paused = await store.load(run_id)
                if paused is not None and paused.agent_paused:
                    break
                await asyncio.sleep(0.01)
            assert paused is not None

        restarted = _workflow_app(store, runner)
        async with restarted.router.lifespan_context(restarted):
            await asyncio.sleep(0.05)
            restored = await store.load(run_id)
            assert restored is not None
            return paused, restored

    paused, restored = asyncio.run(scenario())

    assert paused.agent_paused is True
    assert restored.agent_paused is True
    assert runner.workflow_attempts == 1


def test_manually_interrupted_step_is_not_restarted_after_process_restart() -> None:
    store = InMemoryStateStore()
    runner = BlockingWorkflowRunner()
    first_app = _workflow_app(store, runner)

    async def scenario() -> RunState:
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Interrupt this step.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            detail = await client.get(f"/api/runs/{run_id}")
            instance_id = detail.json()["steps"][0]["agentInstanceId"]
            stopped = await client.delete(
                f"/api/threads/{instance_id}/runs/current"
            )
            assert stopped.status_code == 204

        restarted = _workflow_app(store, runner)
        async with restarted.router.lifespan_context(restarted):
            await asyncio.sleep(0.05)
            state = await store.load(run_id)
            assert state is not None
            return state

    state = asyncio.run(scenario())

    assert state.agent_paused is True
    assert runner.workflow_attempts == 1


def test_human_to_agent_branch_keeps_the_selected_runner_and_is_cancellable() -> None:
    store = InMemoryStateStore()
    default = BlockingWorkflowRunner()
    selected = BlockingWorkflowRunner()
    app = _workflow_app(
        store,
        default,
        workflow_runners={"codex": default, "claude": selected},
        reviewers={"codex": default, "claude": selected},
        workflow_catalog=_human_then_agent_catalog(),
    )

    async def scenario() -> RunState:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "human-then-agent-v1",
                    "prompt": "Continue with the selected provider.",
                    "repository": "acme/api",
                    "runner": "claude",
                },
            )
            run_id = RunId(created.json()["runId"])
            awaiting = await _await_phase(client, run_id, "awaiting_human_review")
            approved = await client.post(
                f"/api/runs/{run_id}/human-review",
                json={"approved": True},
            )
            assert approved.status_code == 200
            await asyncio.wait_for(selected.started.wait(), timeout=1)
            detail = await client.get(f"/api/runs/{run_id}")
            instance_id = detail.json()["steps"][1]["agentInstanceId"]
            stopped = await client.delete(
                f"/api/threads/{instance_id}/runs/current"
            )
            assert stopped.status_code == 204
            state = await store.load(RunId(awaiting.json()["runId"]))
            assert state is not None
            return state

    state = asyncio.run(scenario())

    assert state.runner_name == "claude"
    assert state.agent_paused is True
    assert selected.workflow_attempts == 1
    assert default.workflow_attempts == 0


def test_fail_step_mcp_call_fails_the_active_workflow() -> None:
    store = InMemoryStateStore()
    runner = TerminalToolRunner(
        "fail_step",
        {"summary": "The implementation cannot continue."},
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Attempt the implementation through MCP.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(100):
                reopened = await client.get(f"/api/runs/{run_id}")
                # Answering the tool call and failing the run are separate
                # tasks, so the run can read as failed while the answer is
                # still on its way back to the agent that asked.
                if (
                    reopened.json()["phase"] == "failed"
                    and runner.acknowledgement is not None
                ):
                    break
                await asyncio.sleep(0.01)
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "failed"
    assert reopened.json()["terminalOutcome"] == "failed"
    assert reopened.json()["failureReason"] == "The implementation cannot continue."
    assert reopened.json()["steps"][0]["status"] == "failed"
    assert isinstance(history[-1], RunFailed)
    assert history[-1].agent_run_id is not None
    assert history[-1].mcp_request_id == "workflow-tool-call-1"
    assert runner.cancelled.is_set()
    assert runner.acknowledgement is not None
    assert runner.acknowledgement["result"] == {
        "content": [{"type": "text", "text": "accepted"}],
        "structuredContent": {"accepted": True},
    }


def test_a_failing_reviewer_fails_the_run_after_a_successful_implementation() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented the change.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    reviewer = TerminalToolRunner(
        "fail_step",
        {"summary": "The workspace no longer holds the implemented change."},
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement a change the reviewer cannot inspect.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "failed")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())
    body = reopened.json()

    assert body["phase"] == "failed"
    assert body["terminalOutcome"] == "failed"
    assert body["failureReason"] == (
        "The workspace no longer holds the implemented change."
    )
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][1]["status"] == "failed"
    assert body["pendingHumanReview"] is None
    assert isinstance(history[-1], RunFailed)
    assert history[-1].agent_run_id is not None
    assert reviewer.cancelled.is_set()


def test_a_reviewer_that_omits_a_declared_output_fails_the_run() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented the change.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    # Valid for the implementation step, which declares `pr_url`, and not for
    # the review step, which declares `findings`.
    reviewer = TerminalToolRunner(
        "complete_step",
        {"outcome": "success", "summary": "Looks fine.", "outputs": {}},
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement a change the reviewer reports badly.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "failed")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())
    body = reopened.json()

    assert body["phase"] == "failed"
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][1]["status"] == "failed"
    assert "terminal result" in body["failureReason"]
    assert REVIEW_STEP in body["failureReason"]
    assert isinstance(history[-1], RunFailed)
    # Corrected rather than accepted, and only up to the runtime's limit.
    assert len(reviewer.seen) == 3
    assert reviewer.seen[-1][-1] == Message.user(INVALID_COMPLETION_ERROR)
    assert reviewer.acknowledgement is not None
    assert reviewer.acknowledgement["result"] == {
        "content": [
            {
                "type": "text",
                "text": "step result is missing required outputs: findings",
            }
        ],
        "isError": True,
    }
    assert not reviewer.cancelled.is_set()


def test_create_workflow_run_uses_and_persists_the_selected_runner() -> None:
    store = InMemoryStateStore()
    codex = ConcurrentRunner()
    codex_reviewer = _reviewer()
    claude = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented with Claude.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
        name='"Implement selected provider feature"',
    )
    claude_reviewer = _reviewer(summary="Reviewed with Claude.")
    app = _workflow_app(
        store,
        codex,
        workflow_runners={"codex": codex, "claude": claude},
        reviewers={"codex": codex_reviewer, "claude": claude_reviewer},
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement the feature.",
                    "repository": "acme/api",
                    "runner": "claude",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            return reopened, [
                await store.load_instance(AgentInstanceId(step["agentInstanceId"]))
                for step in reopened.json()["steps"][:2]
            ]

    reopened, instances = asyncio.run(scenario())

    assert reopened.json()["steps"][0]["summary"] == "Implemented with Claude."
    assert reopened.json()["steps"][1]["summary"] == "Reviewed with Claude."
    assert reopened.json()["name"] == "Implement selected provider feature"
    # The provider a run picks answers for both steps, and the other one for
    # neither -- write-enabled for the implementation, read-only for the review.
    assert len(claude.seen) == 2
    assert len(claude_reviewer.seen) == 1
    assert codex.seen == []
    assert codex_reviewer.seen == []
    assert claude.seen[0][0] == Message.user("Implement the feature.")
    assert "Name this workflow" in claude.seen[0][1].content
    assert [instance.runner for instance in instances] == ["claude", "claude"]


@pytest.mark.parametrize(
    "body",
    [
        {"workflowId": "unknown-v1", "prompt": "Task", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "Task"},
        {
            "workflowId": "implementation-review-v1",
            "prompt": "Task",
            "repository": ".",
            "runner": "unknown",
        },
    ],
)
def test_create_workflow_run_rejects_invalid_requests(body: dict[str, str]) -> None:
    store = InMemoryStateStore()
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/runs", json=body)

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert asyncio.run(store.list_runs()) == ()


def test_workflow_conversation_is_nested_under_its_run_not_standalone() -> None:
    store = InMemoryStateStore()
    state = _workflow_state(RunPhase.RUNNING_AGENT, REVIEW_STEP)
    asyncio.run(store.save(state))
    asyncio.run(
        store.create_instance(
            AgentId("implementation-agent"),
            instance_id=AgentInstanceId("implementation-instance"),
            conversation_id=ConversationId("implementation-conversation"),
            workflow_run_id=state.run_id,
            workflow_step_id=IMPLEMENTATION_STEP,
        )
    )
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            runs = await client.get(f"/api/runs/{state.run_id}")
            threads = await client.get("/api/threads")
            thread = await client.get("/api/threads/implementation-instance")
            return runs, threads, thread

    runs, threads, thread = asyncio.run(scenario())

    implementation = runs.json()["steps"][0]
    assert implementation["agentInstanceId"] == "implementation-instance"
    assert implementation["conversationId"] == "implementation-conversation"
    assert implementation["conversationUrl"] == (
        f"/runs/{state.run_id}/conversations/implementation-instance"
    )
    assert threads.json() == {"threads": []}
    assert thread.json()["workflowRunId"] == state.run_id
    assert thread.json()["workflowStepId"] == IMPLEMENTATION_STEP


def test_run_api_presents_human_rejection_as_the_final_decision() -> None:
    store = InMemoryStateStore()
    awaiting = _workflow_state(RunPhase.AWAITING_HUMAN_REVIEW)
    rejection = HumanReviewCompleted(
        run_id=awaiting.run_id,
        step_id=HUMAN_REVIEW_STEP,
        approved=False,
        summary="Address the cancellation finding first.",
    )
    rejected = RunState(
        run_id=awaiting.run_id,
        task_id=awaiting.task_id,
        workflow_id=awaiting.workflow_id,
        phase=RunPhase.FAILED,
        repository=awaiting.repository,
        prompt=awaiting.prompt,
        current_step_id=HUMAN_REVIEW_STEP,
        step_results=awaiting.step_results,
        human_review=rejection,
    )
    asyncio.run(store.save(rejected))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(f"/api/runs/{rejected.run_id}")

    body = asyncio.run(scenario()).json()

    assert body["terminalOutcome"] == "rejected"
    assert body["humanDecision"]["outcome"] == "rejected"
    assert body["humanDecision"]["summary"] == rejection.summary
    assert body["steps"][1]["outcome"] == "changes_requested"


def test_run_id_frontend_route_serves_the_application(tmp_path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/runs/run-42")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


def test_workflow_conversation_frontend_route_serves_the_application(tmp_path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/runs/run-42/conversations/implementation-instance"
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


def test_new_workflow_frontend_route_serves_the_application(tmp_path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/runs/new")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


class ConversationWorkspaces:
    """A provider whose checkouts come and go, as real ones do."""

    def __init__(self) -> None:
        self.count = 0
        self.detached: set[str] = set()
        self.attachments: list[tuple[str, str, str]] = []

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        self.count += 1
        return self._workspace(f"ws-{self.count}", repository, base_ref)

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.detached:
            raise KeyError(f"no workspace {workspace_id!r}")
        return f"/worktrees/{workspace_id}"

    async def state(self, workspace_id: str) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=(
                None if workspace_id in self.detached else f"/worktrees/{workspace_id}"
            ),
        )

    async def attach(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        self.attachments.append((workspace_id, repository, base_ref))
        self.detached.discard(workspace_id)
        return self._workspace(workspace_id, repository, base_ref)

    async def detach(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    async def dispose(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    def _workspace(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/worktrees/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )


class VanishingWorkspaces(ConversationWorkspaces):
    """A provider that has never heard of a workspace the store still names."""

    def __init__(self) -> None:
        super().__init__()
        self.forgotten: set[str] = set()

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().root_path(workspace_id)

    async def state(self, workspace_id: str) -> WorkspaceState:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().state(workspace_id)


def _workspace_session(
    runner: ConcurrentRunner,
    workspaces: ConversationWorkspaces,
    store: InMemoryStateStore | None = None,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces,
            state_store=store if store is not None else InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners={"test": runner},
        workspace_repository="/repository",
    )


def test_each_new_chat_reports_its_own_worktree() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    session = _workspace_session(runner, workspaces)
    app = create_app(session, {"test": runner})

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"agentId": "coder", "runner": "test"}
            first = await client.post("/api/threads", json=body)
            second = await client.post("/api/threads", json=body)
            await client.post(
                f"/api/threads/{first.json()['id']}/runs", json={"text": "inspect"}
            )
            return first.json(), second.json()

    first, second = asyncio.run(scenario())

    assert first["workspaceRoot"] == "/worktrees/ws-1"
    assert second["workspaceRoot"] == "/worktrees/ws-2"
    assert first["workspaceRoot"] != second["workspaceRoot"]
    assert runner.workspace_ids == ["ws-1"]


def test_a_removed_worktree_does_not_take_the_other_chats_with_it() -> None:
    """One vanished checkout used to brick every endpoint, new chats included."""
    runner = ConcurrentRunner()
    workspaces = VanishingWorkspaces()
    store = InMemoryStateStore()
    first_app = create_app(
        _workspace_session(runner, workspaces, store), {"test": runner}
    )

    async def scenario():
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            abandoned = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
        workspaces.forgotten.add("ws-1")

        # A restart: the registry is rebuilt from the store, whose instances
        # still name a workspace that is no longer on disk.
        restarted = create_app(
            _workspace_session(runner, workspaces, store), {"test": runner}
        )
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/threads")
            survivor = await client.get(f"/api/threads/{abandoned.json()['id']}")
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            fresh = await client.get(f"/api/threads/{created.json()['id']}")
        return listed, survivor, created, fresh

    listed, survivor, created, fresh = asyncio.run(scenario())

    assert listed.status_code == 200
    assert survivor.status_code == 200
    assert "workspaceRoot" not in survivor.json()
    assert survivor.json()["workspaceAttached"] is False
    assert created.status_code == 201
    assert fresh.status_code == 200
    assert fresh.json()["workspaceRoot"] == "/worktrees/ws-2"


def test_detaching_keeps_the_work_reachable_and_reattaching_brings_it_back() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = create_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            detached = await client.delete(f"/api/threads/{thread_id}/workspace")
            listed_detached = await client.get(f"/api/threads/{thread_id}")
            reattached = await client.post(f"/api/threads/{thread_id}/workspace")
        return created.json(), detached.json(), listed_detached.json(), reattached.json()

    created, detached, listed, reattached = asyncio.run(scenario())

    assert created["workspaceAttached"] is True
    assert detached["workspaceAttached"] is False
    assert "workspaceRoot" not in detached
    # The work stays addressable while there is nowhere to run it.
    assert detached["workspaceRef"] == "engine/ws-1"
    assert listed["workspaceAttached"] is False
    # Reattaching is the same workspace, not a replacement for it.
    assert reattached["workspaceAttached"] is True
    assert reattached["workspaceRoot"] == created["workspaceRoot"]
    assert reattached["workspaceRef"] == "engine/ws-1"
    assert workspaces.count == 1


def test_workflow_checkout_cannot_detach_while_any_shared_step_is_running() -> None:
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implementation complete.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    reviewer = WorkflowProgressRunner()
    reviewer.arguments["outputs"] = {"findings": "No blocking findings."}
    app = _workflow_app(
        InMemoryStateStore(), implementer, reviewers={"test": reviewer}
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Keep the checkout safe.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(200):
                detail = await client.get(f"/api/runs/{run_id}")
                assert detail.json()["phase"] != "failed", detail.json()[
                    "failureReason"
                ]
                if reviewer.started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert reviewer.started.is_set()
            instance_id = detail.json()["steps"][0]["agentInstanceId"]
            refused = await client.delete(
                f"/api/threads/{instance_id}/workspace"
            )
            reviewer.release.set()
            await _await_phase(client, run_id, "awaiting_human_review")
            detached = await client.delete(
                f"/api/threads/{instance_id}/workspace"
            )
            return refused, detached

    refused, detached = asyncio.run(scenario())

    assert refused.status_code == 409
    assert refused.json()["error"] == "this workflow has a run in progress"
    assert detached.status_code == 200
    assert detached.json()["workspaceAttached"] is False


def test_workflow_checkout_toggle_refreshes_every_shared_conversation() -> None:
    store = InMemoryStateStore()
    workspaces = ConversationWorkspaces()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implementation complete.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    app = _workflow_app(
        store,
        implementer,
        workspaces=workspaces,
        reviewers={"test": _reviewer()},
        workspace_repository="/chat/repository",
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Keep shared checkout state current.",
                    "repository": "acme/workflow-repository",
                },
            )
            run_id = RunId(created.json()["runId"])
            detail = await _await_phase(client, run_id, "awaiting_human_review")
            implementation_id = detail.json()["steps"][0]["agentInstanceId"]
            review_id = detail.json()["steps"][1]["agentInstanceId"]

            # Load both into ThreadService's cache before changing their shared
            # workspace through only one conversation.
            await client.get(f"/api/threads/{implementation_id}")
            await client.get(f"/api/threads/{review_id}")
            detached = await client.delete(
                f"/api/threads/{implementation_id}/workspace"
            )
            review_after_detach = await client.get(f"/api/threads/{review_id}")
            reattached = await client.post(f"/api/threads/{review_id}/workspace")
            implementation_after_attach = await client.get(
                f"/api/threads/{implementation_id}"
            )
            return (
                detached,
                review_after_detach,
                reattached,
                implementation_after_attach,
            )

    detached, review_after_detach, reattached, implementation_after_attach = (
        asyncio.run(scenario())
    )

    assert detached.json()["workspaceAttached"] is False
    assert review_after_detach.json()["workspaceAttached"] is False
    assert reattached.json()["workspaceAttached"] is True
    assert implementation_after_attach.json()["workspaceAttached"] is True
    assert workspaces.attachments == [
        ("ws-1", "acme/workflow-repository", "origin/main")
    ]


def test_detached_workflow_conversation_refuses_continuation_without_failing_run() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implementation complete.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        },
    )
    app = _workflow_app(store, implementer)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Keep detached runs recoverable.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            awaiting = await _await_phase(client, run_id, "awaiting_human_review")
            implementation_id = awaiting.json()["steps"][0]["agentInstanceId"]
            detached = await client.delete(
                f"/api/threads/{implementation_id}/workspace"
            )
            refused = await client.post(
                f"/api/threads/{implementation_id}/runs",
                json={"text": "Continue from here."},
            )
            unchanged = await client.get(f"/api/runs/{run_id}")
            return detached, refused, unchanged

    detached, refused, unchanged = asyncio.run(scenario())

    assert detached.status_code == 200
    assert refused.status_code == 409
    assert "reattach" in refused.json()["error"]
    assert unchanged.json()["phase"] == "awaiting_human_review"
    assert not unchanged.json()["failureReason"]


def test_a_detached_chat_is_told_to_reattach_rather_than_failing_on_a_path() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = create_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.delete(f"/api/threads/{thread_id}/workspace")
            refused = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
            await client.post(f"/api/threads/{thread_id}/workspace")
            accepted = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
        return refused, accepted

    refused, accepted = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "reattach" in refused.json()["error"]
    assert accepted.status_code == 200
    assert runner.workspace_ids == ["ws-1"]


def test_a_chat_that_never_had_a_workspace_can_be_given_one() -> None:
    """Conversations from before worktrees existed, and any other stragglers."""
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    store = InMemoryStateStore()
    session = _workspace_session(runner, workspaces, store)
    app = create_app(session, {"test": runner})

    async def scenario():
        instance = await store.create_instance(CODER)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            before = await client.get(f"/api/threads/{instance.instance_id}")
            attached = await client.post(f"/api/threads/{instance.instance_id}/workspace")
        # The pairing is durable, not just something the page is holding.
        stored = await store.load_instance(instance.instance_id)
        return before.json(), attached.json(), stored

    before, attached, stored = asyncio.run(scenario())

    assert before["workspaceAttached"] is False
    assert "workspaceRef" not in before
    assert attached["workspaceAttached"] is True
    assert attached["workspaceRoot"] == "/worktrees/ws-1"
    assert stored.workspace_id == "ws-1"


def test_a_process_without_a_workspace_repository_says_so() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            return await client.post(f"/api/threads/{created.json()['id']}/workspace")

    refused = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "workspace repository" in refused.json()["error"]


def test_different_chats_can_run_at_the_same_time() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> None:
        first = await service.create(CODER, "test")
        second = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(first.instance_id, "first", None, asyncio.Queue()),
            service.say(second.instance_id, "second", None, asyncio.Queue()),
        )

    asyncio.run(scenario())

    assert runner.most_active == 2


def test_one_chat_serializes_its_own_turns() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> tuple[Message, ...]:
        thread = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(thread.instance_id, "first", None, asyncio.Queue()),
            service.say(thread.instance_id, "second", None, asyncio.Queue()),
        )
        return await service.history(thread.instance_id)

    history = asyncio.run(scenario())

    assert runner.most_active == 1
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "first"),
        (Role.ASSISTANT, "one"),
        (Role.USER, "second"),
        (Role.ASSISTANT, "two"),
    ]


def test_http_api_creates_lists_and_streams_threads() -> None:
    runner = ConcurrentRunner(("hello",))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            config = await client.get("/api/config")
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "hi", "runner": "test"},
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
        return config, created, streamed, messages

    config, created, streamed, messages = asyncio.run(scenario())

    assert config.status_code == 200
    assert config.json()["defaultRunner"] == "test"
    assert config.json()["workflowRunners"] == ["test"]
    assert config.json()["defaultWorkflowRunner"] == "test"
    assert created.status_code == 201
    assert streamed.status_code == 200
    assert '"type":"done"' in streamed.text
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_the_config_names_the_agent_the_plan_button_talks_to() -> None:
    """The client asks which agent plans rather than knowing an id of its own,
    and is told nothing when a composition has no planner to offer."""
    runner = ConcurrentRunner()
    shipped = create_app(_session_with({"test": runner}, BUILT_IN), {"test": runner})
    coders_only = create_app(_session(runner), {"test": runner})

    async def config(app) -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get("/api/config")).json()

    shipped_config, narrow_config = asyncio.run(config(shipped)), asyncio.run(config(coders_only))

    assert shipped_config["planAgent"] == "planner"
    assert "planner" in [agent["id"] for agent in shipped_config["agents"]]
    assert shipped_config["defaultAgent"] == "coder"
    assert narrow_config["planAgent"] == ""


def test_a_chat_keeps_the_runner_it_was_given_for_turns_that_name_none() -> None:
    """The conversation remembers its runner; a turn need not repeat it.

    The header sends the choice once, so a turn that carries no runner has to
    reach whoever the chat was last set to rather than the wired default.
    """
    first = ConcurrentRunner(("from the first",))
    second = ConcurrentRunner(("from the second",))
    runners = {"test": first, "other": second}
    app = create_app(_session_with(runners), runners)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            switched = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "other"}
            )
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "hi"})
            reloaded = await client.get(f"/api/threads/{thread_id}")
            unknown = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "nobody"}
            )
            return switched, reloaded, unknown

    switched, reloaded, unknown = asyncio.run(scenario())

    assert switched.json()["runner"] == "other"
    assert reloaded.json()["runner"] == "other"
    assert [turn[-1].content for turn in second.seen] == ["hi"]
    assert first.seen == []
    assert unknown.status_code == 400


def test_agent_names_chat_before_answer_without_changing_conversation() -> None:
    runner = ConcurrentRunner(('"SQLite Conversation Persistence"', "The answer."))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title",
                json={
                    "text": "Why are chats missing after restart?",
                    "runner": "test",
                },
            )
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "Why are chats missing after restart?"},
            )
            repeated_title = await client.post(
                f"/api/threads/{thread_id}/title", json={}
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return title, repeated_title, messages

    title, repeated_title, messages = asyncio.run(scenario())

    assert title.json() == {"title": "SQLite Conversation Persistence"}
    assert repeated_title.json() == title.json()
    assert runner.seen[0] == (
        Message.user("Why are chats missing after restart?"),
        Message.user(
            "Name this chat based on the conversation above. Reply with only a concise "
            "title of at most eight words, with no quotes or ending punctuation."
        ),
    )
    assert runner.seen[1] == (Message.user("Why are chats missing after restart?"),)
    assert len(runner.seen) == 2
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "Why are chats missing after restart?"),
        ("assistant", "The answer."),
    ]


def test_a_provider_that_cannot_name_a_chat_does_not_cost_the_turn() -> None:
    """Naming happens before the message it names is sent, so it cannot fail it.

    A CLI that is out of quota, unauthenticated, or simply broken fails the
    first thing the client asks of it, which is a title. Answered with a 500
    that would stop the chat working entirely -- for a name.
    """

    class FailsToName(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            if args[2][-1].content.startswith("Name this chat"):
                raise RuntimeError("codex exited 1: stream error: unauthorized")
            return await super().run_turn(*args, **kwargs)

    runner = FailsToName(("The answer.",))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title", json={"text": "hello"}
            )
            run = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "hello"}
            )
            return title, run, await client.get(f"/api/threads/{thread_id}")

    title, run, thread = asyncio.run(scenario())

    assert title.status_code == 200
    assert title.json()["title"] == "New chat"
    # Not silence: the placeholder name says nothing about which provider
    # failed, and somebody reading the response deserves the reason.
    assert "unauthorized" in title.json()["error"]
    # The turn the client was about to send goes through regardless.
    assert run.status_code == 200
    assert thread.json()["title"] == "New chat"
    finished = json.loads([line for line in run.text.splitlines() if line][-1])
    assert finished["type"] == "done"
    assert finished["content"][0]["text"] == "The answer."


def test_missing_frontend_has_an_actionable_response() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert "npm --prefix apps/web run build" in response.text


def test_tool_activity_round_trips_as_assistant_ui_parts() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class ToolRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ToolRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "inspect"})
            return (await client.get(f"/api/threads/{thread_id}/messages")).json()

    content = asyncio.run(scenario())["messages"][1]["content"]

    assert content == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
            "result": "engine",
        },
        {"type": "text", "text": "Found it."},
    ]


def test_a_stopped_run_leaves_its_work_in_the_reloaded_transcript() -> None:
    """Pressing stop ends the turn, not the record of it. What the agent had
    already done is on disk whatever the button does, so a reload that showed
    the question alone would be a transcript the worktree disagrees with."""
    call = ToolCall(call_id="call-1", name="Write", arguments='{"path":"worker.py"}')

    class StoppedMidWorkRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.reported = asyncio.Event()

        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            raise AssertionError("the streaming method should be used")

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            on_message(Message.assistant("Rewriting the worker."))
            on_message(Message.assistant(tool_calls=(call,)))
            self.reported.set()
            await asyncio.Event().wait()
            raise AssertionError("this runner only ever ends by being stopped")

    runner = StoppedMidWorkRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "rewrite it"})
            )
            await runner.reported.wait()
            stopped = await client.delete(f"/api/threads/{thread_id}/runs/current")
            await started
            # A fresh page load: the stream is gone, so this is all the client
            # gets to know about the turn that was stopped.
            return stopped, await client.get(f"/api/threads/{thread_id}/messages")

    stopped, messages = asyncio.run(scenario())

    assert stopped.status_code == 204
    reloaded = messages.json()["messages"]
    # The note the next turn is given is prompt context, not something to show
    # a person, so it does not become a message here.
    assert [message["role"] for message in reloaded] == ["user", "assistant"]
    assert reloaded[1]["content"] == [
        {"type": "text", "text": "Rewriting the worker."},
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Write",
            "args": {"path": "worker.py"},
            "argsText": '{"path":"worker.py"}',
            # Answered rather than left pending, which is what a client shows
            # as a tool still running.
            "result": "interrupted",
        },
    ]


def test_active_run_survives_stream_disconnect_and_replays_progress() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class RefreshRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            tool_call = Message.assistant(tool_calls=(call,))
            tool_result = Message.tool_result(call.call_id, "engine")
            answer = Message.assistant("Found it.")
            on_message(tool_call)
            self.started.set()
            await self.release.wait()
            on_message(tool_result)
            on_message(answer)
            return AgentTurn(answer, steps=(tool_call, tool_result))

    runner = RefreshRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "inspect", None)
        await runner.started.wait()

        original_stream = run.stream()
        first = json.loads((await anext(original_stream)).decode())
        await original_stream.aclose()  # the browser refreshed

        assert service.active_run(thread.instance_id) is run
        active = service.active_run(thread.instance_id)
        assert active is not None
        resumed_stream = active.stream()
        replayed = json.loads((await anext(resumed_stream)).decode())

        runner.release.set()
        events = [replayed]
        async for event in resumed_stream:
            events.append(json.loads(event.decode()))
        return first, events, await service.history(thread.instance_id)

    first, events, history = asyncio.run(scenario())

    assert first["content"] == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
        }
    ]
    assert events[0] == first
    assert events[-1]["type"] == "done"
    assert events[-1]["content"][-1] == {"type": "text", "text": "Found it."}
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "inspect"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "engine"),
        (Role.ASSISTANT, "Found it."),
    ]


def test_the_built_client_is_revalidated_but_its_hashed_assets_are_not(tmp_path) -> None:
    """A cached entry point asks for the assets of a build that is gone."""
    runner = ConcurrentRunner()
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/index-abc123.js"></script>')
    (dist / "assets" / "index-abc123.js").write_text("console.log('engine')")
    app = create_app(_session(runner), {"test": runner}, dist)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                await client.get("/"),
                await client.get("/assets/index-abc123.js"),
            )

    page, asset = asyncio.run(scenario())

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
