"""The engine emits commands rather than invoking adapters.

The architectural claim from the ticket, demonstrated rather than asserted: a
decision is made with no capabilities wired at all, and the resulting command is
dispatched against fakes that satisfy the ports structurally.
"""

import asyncio

from engine.core import decide
from engine.domain import (
    ProvisionWorkspace,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    TaskId,
)
from engine.ports import Workspace, WorkspaceProvider, WorkspaceState
from engine.runtime import Capabilities, Dispatcher, UnhandledCommandError

RUN = RunId("run-1")
TASK = TaskId("task-1")


def test_decide_needs_no_infrastructure() -> None:
    """No clock, no I/O, no capabilities -- just data in, data out."""
    state = RunState(run_id=RUN, task_id=TASK)
    event = RunRequested(
        run_id=RUN, task_id=TASK, prompt="fix the flaky test", repository="acme/api"
    )

    next_state, commands = decide(state, event)

    assert next_state.phase is RunPhase.PREPARING_WORKSPACE
    assert commands == (
        ProvisionWorkspace(run_id=RUN, repository="acme/api", base_ref="origin/main"),
    )


def test_decide_is_pure() -> None:
    """Same inputs, same outputs, and the input state is left alone."""
    state = RunState(run_id=RUN, task_id=TASK)
    event = RunRequested(run_id=RUN, task_id=TASK, prompt="p", repository="acme/api")

    assert decide(state, event) == decide(state, event)
    assert state.phase is RunPhase.PENDING


def test_terminal_runs_emit_nothing() -> None:
    state = RunState(run_id=RUN, task_id=TASK, phase=RunPhase.SUCCEEDED)
    event = RunRequested(run_id=RUN, task_id=TASK, prompt="p", repository="acme/api")

    next_state, commands = decide(state, event)

    assert next_state is state
    assert commands == ()


class FakeWorkspaceProvider:
    """Satisfies `WorkspaceProvider` by shape alone -- no base class, no import
    of the protocol at runtime. That is what makes the ports useful."""

    def __init__(self) -> None:
        self.provisioned: list[tuple[str, str]] = []

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        self.provisioned.append((repository, base_ref))
        return Workspace(
            workspace_id="ws-1", root_path="/tmp/ws-1", repository=repository, base_ref=base_ref
        )

    async def root_path(self, workspace_id: str) -> str:
        return f"/tmp/{workspace_id}"

    async def state(self, workspace_id: str) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=f"/tmp/{workspace_id}",
        )

    async def attach(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/tmp/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )

    async def detach(self, workspace_id: str) -> None:
        pass

    async def dispose(self, workspace_id: str) -> None:
        pass


def _capabilities(workspace_provider: object) -> Capabilities:
    missing = object()  # unused capabilities are never touched by this command
    return Capabilities(
        workflow_runtime=missing,
        source_control=missing,
        agent_runner=missing,
        communications=missing,
        workspace_provider=workspace_provider,
        state_store=missing,
    )


def test_fake_satisfies_the_port_structurally() -> None:
    assert isinstance(FakeWorkspaceProvider(), WorkspaceProvider)


def test_runtime_dispatches_the_command_to_the_capability() -> None:
    provider = FakeWorkspaceProvider()
    dispatcher = Dispatcher(_capabilities(provider))
    _, commands = decide(
        RunState(run_id=RUN, task_id=TASK),
        RunRequested(run_id=RUN, task_id=TASK, prompt="p", repository="acme/api"),
    )

    asyncio.run(dispatcher.dispatch_all(commands))

    assert provider.provisioned == [("acme/api", "origin/main")]


def test_unmapped_command_fails_loudly() -> None:
    """Silent no-ops are how orchestrators lose work."""

    class Unknown:
        run_id = RUN

    dispatcher = Dispatcher(_capabilities(FakeWorkspaceProvider()))
    try:
        asyncio.run(dispatcher.dispatch(Unknown()))
    except UnhandledCommandError as error:
        assert "Unknown" in str(error)
    else:
        raise AssertionError("expected UnhandledCommandError")
