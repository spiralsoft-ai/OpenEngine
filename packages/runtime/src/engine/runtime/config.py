"""Provider-neutral configuration loaded before applications compose adapters.

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
class EngineConfig:
    """All configuration understood by this version of Engine."""

    approvals: ApprovalConfig = ApprovalConfig()
    attribution: bool = True


@dataclass(frozen=True, slots=True)
class LoadedEngineConfig:
    """Validated settings together with the file they came from, if any."""

    config: EngineConfig = EngineConfig()
    path: Path | None = None


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

    _reject_unknown(document, {"attribution", "approvals"}, "configuration")
    attribution = document.get("attribution", True)
    if not isinstance(attribution, bool):
        raise EngineConfigError("attribution must be a boolean")

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

    return EngineConfig(
        attribution=attribution,
        approvals=ApprovalConfig(
            auto_approve=auto_approve,
            allow=tuple(capabilities),
            bash=BashApprovalConfig(
                allow=_patterns(bash.get("allow", ()), "approvals.bash.allow"),
                ask=_patterns(bash.get("ask", ()), "approvals.bash.ask"),
                deny=_patterns(bash.get("deny", ()), "approvals.bash.deny"),
            ),
        )
    )


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
    attribution = "on" if loaded.config.attribution else "off"
    return (
        f"configuration: {source}; attribution={attribution}; approvals enforced "
        f"(auto_approve={auto_approve}, allow={capabilities}, bash_rules={bash_rules})"
    )


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
    "DEFAULT_CONFIG_NAME",
    "EngineConfig",
    "EngineConfigError",
    "LoadedEngineConfig",
    "describe_loaded_config",
    "load_engine_config",
    "parse_engine_config",
]
