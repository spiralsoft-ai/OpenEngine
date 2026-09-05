"""Configuration loaded before applications compose adapters.

This module deliberately stops at reading and validating Engine's vocabulary.
Runners expose provider translators for that vocabulary, while policy
evaluation remains a separate concern.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from engine.ports.agent_runner import ResponseStyle
from engine.ports.permissions import ApprovalCapability

CONFIG_ENVIRONMENT_VARIABLE = "ENGINE_CONFIG"
DEFAULT_CONFIG_NAME = "engine.toml"


class EngineConfigError(ValueError):
    """A configuration file could not be found, parsed, or validated."""


@dataclass(frozen=True, slots=True)
class BashApprovalConfig:
    """Shell patterns grouped by the decision they will eventually produce."""

    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalConfig:
    """Approval policy expressed without naming a provider or provider tool."""

    auto_approve: bool = False
    allow: tuple[ApprovalCapability, ...] = (ApprovalCapability.READ,)
    bash: BashApprovalConfig = BashApprovalConfig()


@dataclass(frozen=True, slots=True)
class WorkflowsConfig:
    """Where trusted repository-owned Python workflow definitions live."""

    directory: str = ""


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """Settings for the local Temporal service owned by the orchestrator."""

    host: str = "127.0.0.1:7233"
    database: str = ".engine/temporal.sqlite3"
    health_check_interval: float = 5.0


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """Settings that only apply when Claude Code is the runner.

    A table of its own because these have no counterpart elsewhere: written at
    the top level they would read as promises Engine cannot keep for every
    provider, and a reader could not tell which of the two they were.
    """

    output_style: ResponseStyle | None = None
    """How Claude should write, or ``None`` to leave its own default.

    Still Engine's vocabulary rather than Claude's spelling -- the adapter owns
    that translation -- but scoped to the one runner that can honour it.
    """


@dataclass(frozen=True, slots=True)
class CommunicationsConfig:
    """Selects the adapter that fulfills the communications capability."""

    provider: str = "slack"
    channel: str = ""


@dataclass(frozen=True, slots=True)
class WorkOrdersConfig:
    """What a work order gets when nobody filled in a form to ask for one.

    Starting one from a chat message means starting it from a sentence, so the
    three answers the web form collects alongside the prompt have to come from
    somewhere. `repository` has no sensible default and is what makes the
    feature available at all: without it a mention is answered with a note
    saying so rather than with a run against a repository nobody named.
    """

    repository: str = ""
    workflow: str = ""
    """Which workflow to run, or empty for the deployment's only one."""
    runner: str = ""
    """Which agent runs it, or empty for the executor's default."""


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """All configuration understood by this version of Engine."""

    default_branch: str = "main"
    github_client_id: str = ""
    github_token: str = ""
    public_url: str = ""
    communications: CommunicationsConfig = CommunicationsConfig()
    work_orders: WorkOrdersConfig = WorkOrdersConfig()
    approvals: ApprovalConfig = ApprovalConfig()
    workflows: WorkflowsConfig = WorkflowsConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    claude: ClaudeConfig = ClaudeConfig()
    attribution: bool = True


@dataclass(frozen=True, slots=True)
class LoadedEngineConfig:
    """Validated settings together with the file they came from, if any."""

    config: EngineConfig = EngineConfig()
    path: Path | None = None

    @property
    def workflows_directory(self) -> Path | None:
        configured = self.config.workflows.directory
        if not configured:
            return None
        base = self.path.parent if self.path is not None else Path.cwd()
        return _relative_to(Path(configured), base).resolve()

    @property
    def orchestrator_database(self) -> Path:
        """Resolve the Temporal database beside the selected configuration."""
        base = self.path.parent if self.path is not None else Path.cwd()
        return _relative_to(Path(self.config.orchestrator.database), base).resolve()


def load_engine_config(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> LoadedEngineConfig:
    """Load the selected TOML file, or return defaults when none is selected.

    Selection is intentionally singular: an explicit path wins over
    ``ENGINE_CONFIG``, which wins over ``engine.toml`` in the current directory.
    Files are not merged, so the effective permission policy always has one
    inspectable source.
    """

    environment = os.environ if environ is None else environ
    directory = Path.cwd() if cwd is None else Path(cwd)
    selected: Path | None

    if explicit_path is not None:
        selected = _relative_to(Path(explicit_path), directory)
    elif configured := environment.get(CONFIG_ENVIRONMENT_VARIABLE):
        selected = _relative_to(Path(configured), directory)
    else:
        default = directory / DEFAULT_CONFIG_NAME
        selected = default if default.is_file() else None

    if selected is None:
        return LoadedEngineConfig()

    path = selected.resolve()
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise EngineConfigError(f"configuration file does not exist: {path}") from error
    except OSError as error:
        raise EngineConfigError(f"cannot read configuration file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise EngineConfigError(f"invalid TOML in {path}: {error}") from error

    return LoadedEngineConfig(config=parse_engine_config(document), path=path)


def parse_engine_config(document: Mapping[str, object]) -> EngineConfig:
    """Validate a decoded TOML document and return immutable settings."""

    _reject_unknown(
        document,
        {
            "attribution",
            "approvals",
            "claude",
            "communications",
            "default_branch",
            "github_client_id",
            "github_token",
            "orchestrator",
            "public_url",
            "work_orders",
            "workflows",
        },
        "configuration",
    )
    attribution = document.get("attribution", True)
    if not isinstance(attribution, bool):
        raise EngineConfigError("attribution must be a boolean")

    default_branch = document.get("default_branch", "main")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise EngineConfigError("default_branch must be a non-empty string")

    github_client_id = _optional_nonblank_string(
        document.get("github_client_id", ""), "github_client_id"
    )
    github_token = _optional_nonblank_string(
        document.get("github_token", ""), "github_token"
    )
    public_url = _optional_nonblank_string(document.get("public_url", ""), "public_url")

    communications = _table(document.get("communications", {}), "communications")
    _reject_unknown(communications, {"channel", "provider"}, "communications")
    communications_provider = _nonblank_string(
        communications.get("provider", "slack"), "communications.provider"
    )
    if communications_provider not in {"buzz", "slack"}:
        raise EngineConfigError(
            "communications.provider is unknown: "
            f"{communications_provider!r}; expected one of: buzz, slack"
        )
    communications_channel = _optional_nonblank_string(
        communications.get("channel", ""), "communications.channel"
    )

    work_orders = _table(document.get("work_orders", {}), "work_orders")
    _reject_unknown(work_orders, {"repository", "runner", "workflow"}, "work_orders")
    work_order_repository = _optional_nonblank_string(
        work_orders.get("repository", ""), "work_orders.repository"
    )
    work_order_workflow = _optional_nonblank_string(
        work_orders.get("workflow", ""), "work_orders.workflow"
    )
    work_order_runner = _optional_nonblank_string(
        work_orders.get("runner", ""), "work_orders.runner"
    )

    claude = _table(document.get("claude", {}), "claude")
    _reject_unknown(claude, {"output_style"}, "claude")
    output_style = _output_style(claude.get("output_style", ""))

    approvals = _table(document.get("approvals", {}), "approvals")
    _reject_unknown(approvals, {"auto_approve", "allow", "bash"}, "approvals")

    auto_approve = approvals.get("auto_approve", False)
    if not isinstance(auto_approve, bool):
        raise EngineConfigError("approvals.auto_approve must be a boolean")

    capability_names = _strings(approvals.get("allow", ("read",)), "approvals.allow")
    capabilities: list[ApprovalCapability] = []
    for name in capability_names:
        try:
            capabilities.append(ApprovalCapability(name))
        except ValueError as error:
            choices = ", ".join(capability.value for capability in ApprovalCapability)
            raise EngineConfigError(
                f"approvals.allow contains unknown capability {name!r}; expected one of: {choices}"
            ) from error

    bash = _table(approvals.get("bash", {}), "approvals.bash")
    _reject_unknown(bash, {"allow", "ask", "deny"}, "approvals.bash")

    workflows = _table(document.get("workflows", {}), "workflows")
    _reject_unknown(workflows, {"directory"}, "workflows")
    workflow_directory = workflows.get("directory", "")
    if not isinstance(workflow_directory, str):
        raise EngineConfigError("workflows.directory must be a string")
    if workflow_directory and not workflow_directory.strip():
        raise EngineConfigError("workflows.directory must not be blank")

    orchestrator = _table(document.get("orchestrator", {}), "orchestrator")
    _reject_unknown(
        orchestrator, {"host", "database", "health_check_interval"}, "orchestrator"
    )
    orchestrator_host = _nonblank_string(
        orchestrator.get("host", "127.0.0.1:7233"), "orchestrator.host"
    )
    orchestrator_database = _nonblank_string(
        orchestrator.get("database", ".engine/temporal.sqlite3"),
        "orchestrator.database",
    )
    health_check_interval = orchestrator.get("health_check_interval", 5.0)
    if (
        not isinstance(health_check_interval, (int, float))
        or isinstance(health_check_interval, bool)
        or health_check_interval <= 0
    ):
        raise EngineConfigError(
            "orchestrator.health_check_interval must be a positive number"
        )

    return EngineConfig(
        attribution=attribution,
        default_branch=default_branch,
        github_client_id=github_client_id,
        github_token=github_token,
        public_url=public_url.rstrip("/"),
        communications=CommunicationsConfig(
            provider=communications_provider,
            channel=communications_channel,
        ),
        work_orders=WorkOrdersConfig(
            repository=work_order_repository,
            workflow=work_order_workflow,
            runner=work_order_runner,
        ),
        claude=ClaudeConfig(output_style=output_style),
        approvals=ApprovalConfig(
            auto_approve=auto_approve,
            allow=tuple(capabilities),
            bash=BashApprovalConfig(
                allow=_patterns(bash.get("allow", ()), "approvals.bash.allow"),
                ask=_patterns(bash.get("ask", ()), "approvals.bash.ask"),
                deny=_patterns(bash.get("deny", ()), "approvals.bash.deny"),
            ),
        ),
        workflows=WorkflowsConfig(directory=workflow_directory),
        orchestrator=OrchestratorConfig(
            host=orchestrator_host,
            database=orchestrator_database,
            health_check_interval=float(health_check_interval),
        ),
    )


def _optional_nonblank_string(value: object, name: str) -> str:
    """Validate a string setting which may be omitted but never whitespace."""

    if not isinstance(value, str):
        raise EngineConfigError(f"{name} must be a string")
    if value and not value.strip():
        raise EngineConfigError(f"{name} must not be blank")
    return value.strip()


def _nonblank_string(value: object, name: str) -> str:
    value = _optional_nonblank_string(value, name)
    if not value:
        raise EngineConfigError(f"{name} must not be blank")
    return value


def describe_loaded_config(loaded: LoadedEngineConfig) -> str:
    """A compact startup description of the policy this process will apply."""

    source = str(loaded.path) if loaded.path is not None else "defaults (no engine.toml)"
    approvals = loaded.config.approvals
    capabilities = ", ".join(capability.value for capability in approvals.allow) or "none"
    bash_rules = sum(
        len(patterns)
        for patterns in (approvals.bash.allow, approvals.bash.ask, approvals.bash.deny)
    )
    auto_approve = "on" if approvals.auto_approve else "off"
    workflows = (
        str(loaded.workflows_directory)
        if loaded.workflows_directory is not None
        else "disabled"
    )
    attribution = "on" if loaded.config.attribution else "off"
    default_branch = loaded.config.default_branch
    style = loaded.config.claude.output_style
    output_style = style.value if style is not None else "provider default"
    return (
        f"configuration: {source}; attribution={attribution}; default_branch={default_branch}; "
        f"claude.output_style={output_style}; approvals enforced "
        f"(auto_approve={auto_approve}, allow={capabilities}, bash_rules={bash_rules}); "
        f"workflows={workflows}"
    )


def _output_style(value: object) -> ResponseStyle | None:
    """Validated here rather than passed through, because a provider that does
    not recognize a style name may ignore it instead of refusing it -- and a
    misspelled style that quietly does nothing is the one failure a strict
    configuration file exists to prevent."""
    if not isinstance(value, str):
        raise EngineConfigError("claude.output_style must be a string")
    if not value:
        return None
    try:
        return ResponseStyle(value)
    except ValueError as error:
        choices = ", ".join(style.value for style in ResponseStyle)
        raise EngineConfigError(
            f"claude.output_style is unknown: {value!r}; expected one of: {choices}"
        ) from error


def _relative_to(path: Path, directory: Path) -> Path:
    return path if path.is_absolute() else directory / path


def _table(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise EngineConfigError(f"{location} must be a TOML table")
    return value


def _strings(value: object, location: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EngineConfigError(f"{location} must be an array of strings")
    strings = tuple(value)
    if not all(isinstance(item, str) for item in strings):
        raise EngineConfigError(f"{location} must be an array of strings")
    if len(set(strings)) != len(strings):
        raise EngineConfigError(f"{location} must not contain duplicates")
    return strings


def _patterns(value: object, location: str) -> tuple[str, ...]:
    patterns = _strings(value, location)
    if any(not pattern.strip() for pattern in patterns):
        raise EngineConfigError(f"{location} must not contain empty patterns")
    return patterns


def _reject_unknown(
    values: Mapping[str, object], allowed: set[str], location: str
) -> None:
    if unknown := sorted(set(values) - allowed):
        raise EngineConfigError(f"unknown key in {location}: {unknown[0]}")


__all__ = [
    "ApprovalCapability",
    "ApprovalConfig",
    "BashApprovalConfig",
    "CONFIG_ENVIRONMENT_VARIABLE",
    "ClaudeConfig",
    "DEFAULT_CONFIG_NAME",
    "EngineConfig",
    "EngineConfigError",
    "LoadedEngineConfig",
    "ResponseStyle",
    "WorkOrdersConfig",
    "WorkflowsConfig",
    "describe_loaded_config",
    "load_engine_config",
    "parse_engine_config",
]
