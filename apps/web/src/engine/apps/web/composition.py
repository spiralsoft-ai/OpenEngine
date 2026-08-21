"""Composition root for the web control interface.

The one file in this app allowed to name concrete adapters, and a sibling of the
other two compositions rather than shared code with them -- for the same reason
the worker's is: these processes will diverge, and sharing now would couple
three deployables that should be free to move independently.

Three of the six capabilities here are real. `agent_runner` shells out to a
coding CLI, `state_store` persists conversations in SQLite, and
`workspace_provider` gives every chat an isolated Git worktree. The other three
remain wired for the composition report but are not exposed by the chat API.

`Capabilities` holds one runner because a port has one implementation, and that
is the one anything non-interactive uses. The interface additionally offers a
*choice* of runner, which is `build_runners` -- a name-to-implementation mapping
of exactly the kind a composition root exists to own.

The state store is SQLite rather than Postgres: conversations survive a process
restart without requiring an external database service.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.adapters.agent_runner.claude_code import (
    READ_ONLY_TOOLS,
    WORKSPACE_WRITE_TOOLS,
    ClaudeCodeAgentRunner,
    allowed_tools_for,
)
from engine.adapters.agent_runner.codex import CodexAgentRunner
from engine.adapters.communications.buzz import BuzzCommunications
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.ports import AgentRunner
from engine.runtime import AgentSession, Capabilities, EngineConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the interface needs from the environment.

    `host` and `port` are handed to Uvicorn by `__main__`; the rest are adapter
    arguments. Loading them from the environment lands with the deployment
    ticket, along with the other two roots.

    Frozen so one immutable settings value can be shared by the server wiring.
    """

    host: str = "localhost"
    port: int = 8000
    codex_binary: str = "codex"
    codex_sandbox: str = "read-only"
    """What a turn nobody is watching may do: read, and nothing else.

    `build_capabilities` wires the one runner a non-interactive caller reaches
    for, so it gets the sandbox that needs no one present. Chat is the other
    case and takes `interactive_codex_sandbox`.
    """
    codex_working_directory: str = "."
    codex_timeout_seconds: float | None = None
    """No ceiling: a turn runs until it is done or someone cancels it."""
    codex_model: str = ""
    claude_binary: str = "claude"
    claude_working_directory: str = "."
    claude_timeout_seconds: float | None = None
    """Same as `codex_timeout_seconds`."""
    claude_model: str = ""
    interactive_codex_sandbox: str = "workspace-write"
    """An approved command has to be able to do the thing it was approved for.

    The read-only sandbox would refuse the write after the user allowed it, and
    on-request approval is what keeps that from meaning "unattended": Codex
    stops and asks before stepping outside the worktree.

    So this is not built from `approvals.allow`, and cannot be: a capability
    that policy leaves unruled is one a person may still allow mid-turn, and a
    sandbox narrowed before the turn started would refuse what they just
    allowed. Codex's policy is applied to its requests instead.
    """
    workflow_codex_sandbox: str = "workspace-write"
    """Workflow implementation may edit only its isolated worktree."""
    workflow_claude_allowed_tools: tuple[str, ...] = WORKSPACE_WRITE_TOOLS
    """Claude workflows may read and edit files, but not run unrestricted Bash."""
    temporal_host: str = "localhost:7233"
    github_token: str = ""
    github_binary: str = "gh"
    """The GitHub CLI review comments are left with.

    A field for the same reason `codex_binary` and `claude_binary` are: which
    executable a capability shells out to is the deployment's to state, and
    stating it here keeps it readable in the one file allowed to name adapters.
    A `PATH` shim would do the same job invisibly, and would take the next thing
    in the process that wanted the real `gh` with it.
    """
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = "/tmp/engine-workspaces"
    sqlite_path: str = "conversations.sqlite3"
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML.

    `approvals` is where chat's permissions are written down: it builds the
    interactive Claude runner's tool list, and answers both runners' approval
    requests. No field here holds a second copy of it.
    """
    config_path: Path | None = None
    """The single TOML source, or ``None`` when built-in defaults are active."""


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=GitHubSourceControl(
            settings.github_token, binary_path=settings.github_binary
        ),
        agent_runner=CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=workspace_provider,
        state_store=SQLiteStateStore(settings.sqlite_path),
    )


def build_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Every agent runner this process offers, by the name the interface shows.

    The one place a runner name is bound to an implementation -- below this file
    "codex" and "claude" are opaque strings, exactly like tool grants. The first
    entry is the default, so it is also what a conversation gets when nobody
    picks.

    One entry per CLI: the dropdown names the agent you are talking to, not the
    transport it is driven over. Both pause for approval, because a runner that
    could only run unattended is not a second choice worth offering -- what it
    would do without asking, these do after asking.

    What Claude may do without asking is `approvals.allow` from `engine.toml`:
    a preapproved tool is one whose requests never reach the callback, and one
    left off the list is still allowed if a person allows it. Codex has no such
    list -- its pre-turn knob is a sandbox, which is a ceiling rather than a
    preapproval -- so its whole policy is applied to its requests instead. Shell
    is on neither list on purpose: a shell rule is written per command rather
    than per capability, so `Bash` reaches the callback either way and
    `approvals.bash` is applied there.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.interactive_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": ClaudeCodeAgentRunner(
            binary_path=settings.claude_binary,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=allowed_tools_for(settings.engine_config.approvals.allow),
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
    }


def build_review_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Read-only runners: workflow review steps, and agents that only read.

    Not built from `approvals.allow`, and deliberately: a reviewer that cannot
    write is a property of the step rather than a permission the deployment gets
    to widen. A policy granting `edit` is a statement about what an agent may do
    when somebody asks it to change something, not about the one asked to read.

    The same holds for a role that never changes anything -- the planner --
    which is why `build_session` is handed this map too.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": ClaudeCodeAgentRunner(
            binary_path=settings.claude_binary,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=READ_ONLY_TOOLS,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
    }


def build_workflow_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Write-enabled runners used only for workflow implementation steps.

    Named to match `build_review_runners` on purpose, and a subset of it: a run
    implements with the write-enabled runner of the provider it picked, and is
    then reviewed by the read-only runner of that same name. The reviewer is
    told not to modify the workspace, but what actually stops it is being run
    without the tools to.

    Structural for the same reason its sibling is: an implementation step exists
    to change the tree it was given, and a policy that had not thought about
    workflows would otherwise silently produce one that cannot. The approvals it
    raises are still governed -- those go through the broker like any other.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.workflow_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": ClaudeCodeAgentRunner(
            binary_path=settings.claude_binary,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=settings.workflow_claude_allowed_tools,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
    }


def build_session(
    capabilities: Capabilities,
    runners: Mapping[str, AgentRunner],
    repository: str = ".",
    read_only_runners: Mapping[str, AgentRunner] | None = None,
) -> AgentSession:
    """Conversations, over the capabilities this process composed.

    Takes the capability set rather than settings so the interface and the chat
    share one store -- two `build_capabilities` calls would open independent
    connections rather than sharing the session's store object.

    A conversation may be continued by any of `runners`, including one that did
    not start it: we hold the transcript, so whichever answers next is handed
    everything the other one said and did.

    `read_only_runners` answers the agents that only read, by the same provider
    names -- so a planning conversation is the CLI the user picked, without the
    tools to change the tree it is reading.
    """
    return AgentSession(
        capabilities,
        runners=runners,
        workspace_repository=repository,
        read_only_runners=read_only_runners,
    )


__all__ = [
    "Settings",
    "build_capabilities",
    "build_review_runners",
    "build_runners",
    "build_workflow_runners",
    "build_session",
]
