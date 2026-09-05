"""Starting a work order by pinging the bot, and reporting it back.

Four things have to hold for the feature to be what it says it is: a mention
becomes a run, the run remembers where it came from, the agent can say
something mid-step, and the endings -- complete, fail, clarify, review ready --
arrive in the same thread.
"""

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest

from engine.adapters.communications.slack import (
    SlackCredentials,
    SlackCredentialStore,
    mention_from_event,
    verify_signature,
)
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentRunId,
    RunId,
    RunOrigin,
    RunRequested,
    RunState,
    StepId,
    StepSpec,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.ports import AgentTurn, Message as CommunicationsMessage
from engine.runtime import RunNotifier, WorkOrdersConfig
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


SIGNING_SECRET = "shhh"


def _signed(body: bytes, secret: str = SIGNING_SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
        "content-type": "application/json",
    }


# --- reading a delivery ------------------------------------------------------


def test_mention_becomes_a_request_without_the_bot_token() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT|openengine> please add a   health endpoint",
            },
        }
    )
    assert mention is not None
    assert mention.text == "please add a health endpoint"
    assert (mention.channel, mention.author) == ("C123", "U777")
    # No thread yet, so the mention itself is the thread to answer under.
    assert mention.thread_id == "1700.0001"


def test_mention_inside_a_thread_answers_that_thread() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0009",
                "thread_ts": "1700.0001",
                "text": "<@UBOT> do it",
            },
        }
    )
    assert mention is not None and mention.thread_id == "1700.0001"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "url_verification", "challenge": "abc"},
        {"type": "event_callback", "event": {"type": "message", "text": "hi"}},
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "bot_id": "B1",
                "text": "<@UBOT> loop",
            },
        },
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT>",
            },
        },
    ],
    ids=["handshake", "other-event", "the-bot-itself", "nothing-asked"],
)
def test_deliveries_that_are_not_a_request(payload: dict) -> None:
    assert mention_from_event(payload) is None


def test_signature_accepts_slack_and_refuses_everything_else() -> None:
    body = b'{"type":"event_callback"}'
    headers = _signed(body)
    assert verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        "another-secret",
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body + b" ",
    )
    # A capture replayed an hour later is refused even though it verifies.
    stale = str(int(time.time()) - 3600)
    replayed = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + stale.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    assert not verify_signature(SIGNING_SECRET, stale, replayed, body)


# --- the endpoint ------------------------------------------------------------


class RecordingCommunications:
    """A chat provider that remembers what was said and where."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, CommunicationsMessage | str, str]] = []

    async def post(self, channel, message, run_id=None, thread_id="") -> str:
        self.posts.append((channel, message, thread_id))
        return "1700.0002"

    async def reply(self, message_id: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError


def _app(tmp_path, communications, work_orders: WorkOrdersConfig, catalog=None):
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities, WorkflowCatalog

    stub = object()
    capabilities = Capabilities(
        workflow_runtime=stub,
        source_control=stub,
        agent_runner=stub,
        communications=communications,
        workspace_provider=stub,
        state_store=InMemoryStateStore(),
    )
    runners = {"default": stub}
    session = AgentSession(capabilities, profiles={}, runners=runners)
    slack_store = MagicMock(spec=SlackCredentialStore)
    slack_store.credentials.return_value = SlackCredentials("client", "secret")
    slack_store.token.return_value = "xoxb-token"
    slack_store.signing_secret.return_value = SIGNING_SECRET
    return create_app(
        session,
        runners,
        workflow_runners=runners,
        review_runners=runners,
        workflow_catalog=(
            catalog if catalog is not None else WorkflowCatalog.from_definitions(())
        ),
        slack_credential_store=slack_store,
        public_url="https://engine.example",
        work_orders=work_orders,
        credential_store=MagicMock(),
    ), capabilities


def _workflow_catalog():
    import openengine as oe
    from engine.runtime import WorkflowCatalog

    coder = oe.agent(id="coder", instructions="Implement it.")
    return WorkflowCatalog.from_definitions(
        [
            oe.workflow(
                id="implementation-review-v1",
                name="Implementation review",
                version="v1",
                steps=[
                    oe.agent_step(
                        id="implementation",
                        name="Implementation",
                        agent=coder,
                        prompt=oe.template("{task}", task=oe.task.prompt),
                        transitions={"*": oe.succeed()},
                    )
                ],
            )
        ]
    )


def test_handshake_is_answered_with_the_challenge(tmp_path) -> None:
    from starlette.testclient import TestClient

    app, _ = _app(tmp_path, RecordingCommunications(), WorkOrdersConfig())
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc"}


def test_an_unsigned_delivery_starts_nothing(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1"),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT> do something",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=not-a-signature",
            },
        )
    assert response.status_code == 401
    assert communications.posts == []
    assert asyncio.run(capabilities.state_store.list_runs()) == ()


def test_a_mention_starts_a_work_order_and_replies_in_the_thread(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))

    assert response.status_code == 200
    runs = asyncio.run(capabilities.state_store.list_runs())
    assert len(runs) == 1
    state = runs[0]
    assert state.prompt == "add a health endpoint"
    assert state.repository == "acme/api"
    assert state.origin == RunOrigin(
        channel="C123", thread_id="1700.0001", author="U777"
    )

    channel, message, thread_id = communications.posts[0]
    assert (channel, thread_id) == ("C123", "1700.0001")
    assert message.mention == "U777"
    assert "acme/api" in message.text
    assert message.links[0].url == f"https://engine.example/runs/{state.run_id}"


def test_a_redelivery_does_not_start_the_work_order_twice(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        client.post("/api/slack/events", content=body, headers=_signed(body))
        retry = client.post(
            "/api/slack/events",
            content=body,
            headers={**_signed(body), "x-slack-retry-num": "1"},
        )

    assert retry.status_code == 200
    assert len(asyncio.run(capabilities.state_store.list_runs())) == 1


def test_a_mention_with_no_repository_configured_says_so(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities = _app(
        tmp_path, communications, WorkOrdersConfig(), _workflow_catalog()
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert asyncio.run(capabilities.state_store.list_runs()) == ()
    _channel, message, thread_id = communications.posts[0]
    assert thread_id == "1700.0001"
    assert "work_orders.repository" in message.text


# --- reporting back ----------------------------------------------------------


def test_update_status_is_served_only_to_a_run_with_somewhere_to_report() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        async def report(status: str) -> None:
            reported.append(status)

        step = StepSpec(StepId("implementation"), AgentId("coder"))
        silent = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        async with silent:
            assert "--status-updates" not in silent.config.args
            refused = await silent._submit(
                {
                    "token": silent._token,
                    "request_id": 1,
                    "name": "update_status",
                    "arguments": {"status": "working on it"},
                }
            )
        assert refused["ok"] is False

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-2"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(report)
        async with broker:
            assert "--status-updates" in broker.config.args
            accepted = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 2,
                    "name": "update_status",
                    "arguments": {"status": "reading the code"},
                }
            )
            blank = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 3,
                    "name": "update_status",
                    "arguments": {"status": "  "},
                }
            )
        assert accepted["ok"] is True
        assert blank["ok"] is False
        assert reported == ["reading the code"]

    asyncio.run(scenario())


def test_clarify_is_reported_because_no_event_carries_it() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder")),
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(lambda status: _record(reported, status))
        async with broker:
            response = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 1,
                    "name": "clarify",
                    "arguments": {},
                }
            )
        assert response["acknowledgement"] == "clarified"
        assert reported == ["answered a question without changing the work order"]

    asyncio.run(scenario())


async def _record(sink: list[str], status: str) -> None:
    sink.append(status)


def test_a_run_from_the_web_is_never_announced() -> None:
    communications = RecordingCommunications()
    notifier = RunNotifier(communications, "https://engine.example")
    state = RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
    )
    asyncio.run(notifier.announce(state, "half way there"))
    assert communications.posts == []


class CompletingMcpRunner:
    """A runner that completes each step through the real run-bound server.

    Enough of an agent to exercise the reporting path end to end: it posts one
    status, then completes, declaring `pr_url` on the step that has it.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, pull_request_url: str) -> None:
        self._pull_request_url = pull_request_url

    async def run_turn(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("this test drives the MCP path")

    async def run_turn_with_mcp(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        workspace_id=None,
    ):
        from engine.domain import Message as ChatMessage

        outputs = (
            {"pr_url": self._pull_request_url}
            if "implementation" in str(agent_run_id)
            else {}
        )
        await _call_bound_tool(
            mcp_server, "update_status", {"status": "reading the code"}, "call-1"
        )
        await _call_bound_tool(
            mcp_server,
            "complete_step",
            {"outcome": "success", "summary": "Done.", "outputs": outputs},
            "call-2",
        )
        await asyncio.sleep(0)
        return AgentTurn(ChatMessage.assistant("Completed."))

    async def cancel(self, _agent_run_id) -> None:
        return None


async def _call_bound_tool(mcp_server, name, arguments, request_id):
    host = mcp_server.args[mcp_server.args.index("--host") + 1]
    port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
    token = mcp_server.args[mcp_server.args.index("--token") + 1]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    assert response["ok"] is True, response
    return response


class OneWorkspaceProvider:
    async def provision(self, repository: str, base_ref: str):
        from engine.ports import Workspace

        return Workspace(
            workspace_id=WorkspaceId("ws-1"),
            root_path="/tmp/ws-1",
            repository=repository,
            base_ref=base_ref,
        )


def _reporting_workflow():
    import openengine as oe

    coder = oe.agent(id="coder", instructions="Implement it.")
    reviewer = oe.agent(id="reviewer", instructions="Review it.")
    implementation = oe.result("implementation")
    return oe.workflow(
        id="implementation-review-v1",
        name="Implementation review",
        version="v1",
        workspace=oe.workspace(base_ref="origin/main"),
        steps=[
            oe.agent_step(
                id="implementation",
                name="Implementation",
                agent=coder,
                prompt=oe.template("{task}", task=oe.task.prompt),
                required_outputs=["pr_url"],
                workspace_access="write",
                transitions={"success": oe.goto("review"), "*": oe.fail()},
            ),
            oe.agent_step(
                id="review",
                name="Review",
                agent=reviewer,
                prompt=oe.template("Review {pr}", pr=implementation.outputs),
                workspace_access="read",
                transitions={"*": oe.goto("human-review")},
            ),
            oe.human_review_step(
                id="human-review",
                name="Human review",
                title=oe.template("Review {task_id}", task_id=oe.task.id),
                summary=oe.template("done"),
                approved=oe.succeed(),
                rejected=oe.fail(),
                notification=oe.slack_notification(),
            ),
        ],
    )


def test_a_work_order_reports_its_whole_life_in_the_thread() -> None:
    from engine.runtime import Capabilities, WorkflowCatalog, WorkflowExecutor

    pull_request = "https://example.invalid/pr/9"
    communications = RecordingCommunications()
    store = InMemoryStateStore()
    definition = _reporting_workflow()
    runner = CompletingMcpRunner(pull_request)
    capabilities = Capabilities(
        workflow_runtime=object(),
        source_control=object(),
        agent_runner=runner,
        communications=communications,
        workspace_provider=OneWorkspaceProvider(),
        state_store=store,
    )
    executor = WorkflowExecutor(
        capabilities,
        {"default": runner},
        review_runners={"default": runner},
        catalog=WorkflowCatalog.from_definitions([definition]),
        public_url="https://engine.example",
    )
    run_id = RunId("run-1")
    origin = RunOrigin(channel="C123", thread_id="1700.0001", author="U777")

    async def scenario() -> None:
        await store.save(
            RunState(
                run_id=run_id,
                task_id=TaskId("task-1"),
                workflow_id=definition.workflow_id,
                prompt="add a health endpoint",
                repository="acme/api",
                origin=origin,
            )
        )
        await executor.start(
            RunRequested(
                run_id=run_id,
                task_id=TaskId("task-1"),
                prompt="add a health endpoint",
                repository="acme/api",
                workflow_id=definition.workflow_id,
            ),
            "default",
        )

    asyncio.run(scenario())

    said = [message.text for _channel, message, _thread in communications.posts]
    assert all(thread == "1700.0001" for _c, _m, thread in communications.posts)
    assert "*Implementation* started." in said
    assert "*Implementation*: reading the code" in said
    assert any(text.startswith("*Implementation* complete.") for text in said)
    # The review stage announces itself, which is the point of announcing on
    # entry rather than only on ending.
    assert "*Review* started." in said

    completion = next(
        message
        for _channel, message, _thread in communications.posts
        if message.text.startswith("*Implementation* complete.")
    )
    assert [link.url for link in completion.links] == [
        pull_request,
        f"https://engine.example/runs/{run_id}",
    ]

    # The last word is addressed to whoever asked, because it is their decision
    # the run is now waiting on.
    ready = communications.posts[-1][1]
    assert ready.mention == "U777"
    assert "ready for your decision" in ready.text.lower()
    assert pull_request in [link.url for link in ready.links]


def test_a_provider_that_is_down_does_not_break_the_run() -> None:
    class BrokenCommunications:
        async def post(self, *_args, **_kwargs) -> str:
            raise RuntimeError("Slack is unavailable")

        async def reply(self, *_args) -> str:  # pragma: no cover
            raise NotImplementedError

    notifier = RunNotifier(BrokenCommunications(), "https://engine.example")
    state = RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
        origin=RunOrigin(channel="C1", thread_id="1700.0001", author="U1"),
    )
    asyncio.run(notifier.announce(state, "half way there"))
