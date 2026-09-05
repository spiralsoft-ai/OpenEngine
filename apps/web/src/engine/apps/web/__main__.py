"""Web control interface entrypoint.

The Python process serves both the chat API and the built assistant-ui client.
``--check`` retains the cheap composition smoke test used in CI.

Composition is reachable twice: ``main`` runs it once and serves the result,
and ``build_app`` is the import string the development server's reloader names,
which constructs the same application again in every fresh child process.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from starlette.applications import Starlette

from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_graph_runtime,
    build_read_only_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.apps.web.github_auth import GitHubCredentialStore
from engine.adapters.communications.slack import SlackCredentialStore
from engine.apps.web.source_control import SourceControlPreferences
from engine.runtime import (
    EngineConfigError,
    LoadedEngineConfig,
    WorkflowCatalog,
    describe_loaded_config,
    load_engine_config,
    load_workflow_catalog,
    WorkflowLoadError,
)

#: Vite's production output, served by the same process as the API.
STATIC_DIRECTORY = Path(__file__).resolve().parents[4] / "dist"


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
    print(
        describe_loaded_config(
            LoadedEngineConfig(config=settings.engine_config, path=settings.config_path)
        )
    )
    print(f"openengine web -- http://{settings.host}:{settings.port}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print(f"agents: {', '.join(sorted(session.profiles))}")
    print(f"runners: {', '.join(f'{n} ({type(r).__name__})' for n, r in runners.items())}")
    print(
        "workflow runners: "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in workflow_runners.items()
        )
    )
    # Named for what they are and what uses them: an operator reading this has
    # to be able to see that a planning chat runs on these too, not only a
    # workflow's review step.
    print(
        "read-only runners (workflow reviews, read-only agents): "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in read_only_runners.items()
        )
    )
    read_only_agents = sorted(
        agent_id for agent_id, profile in session.profiles.items() if profile.read_only
    )
    print(f"read-only agents: {', '.join(read_only_agents) or 'none'}")
    print(f"assistant-ui chat is live; conversations are stored in {settings.sqlite_path}.")


def _settings(loaded: LoadedEngineConfig) -> Settings:
    """Apply deployment overrides to the immutable TOML configuration."""

    return Settings(
        engine_config=loaded.config,
        config_path=loaded.path,
        github_client_id=os.environ.get(
            "GITHUB_CLIENT_ID", loaded.config.github_client_id
        ),
        github_token=os.environ.get("GITHUB_TOKEN", loaded.config.github_token),
        source_control_preferences=SourceControlPreferences(),
    )


def _github_client_id_source() -> str:
    return "environment" if "GITHUB_CLIENT_ID" in os.environ else "configuration"


def read_configuration(
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[LoadedEngineConfig, WorkflowCatalog | None]:
    """The two files this process reads once, at startup, and never again.

    Together because they fail together -- neither is worth starting without --
    and because "what a restart is for" has to be one list: the development
    server watches exactly what this function reads.
    """
    loaded = load_engine_config(config_path)
    catalog = (
        load_workflow_catalog(loaded.workflows_directory)
        if loaded.workflows_directory is not None
        else None
    )
    return loaded, catalog


def compose_app(
    loaded: LoadedEngineConfig, workflow_catalog: WorkflowCatalog | None
) -> Starlette:
    """Wire the capability graph and hand it to the HTTP surface."""
    settings = _settings(loaded)
    credential_store = GitHubCredentialStore()
    slack_credential_store = SlackCredentialStore()
    capabilities = build_capabilities(
        settings,
        credential_store=credential_store,
        slack_credential_store=slack_credential_store,
    )
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
    # The second engine, for the `[BETA]` workflows in the same directory. It
    # is `None` when that directory holds no graphs, and then the interface
    # offers none of them.
    graph_runtime = build_graph_runtime(
        settings, workflow_catalog.graphs if workflow_catalog is not None else ()
    )
    return create_app(
        session,
        runners,
        STATIC_DIRECTORY,
        workflow_runners=workflow_runners,
        review_runners=read_only_runners,
        workflow_catalog=workflow_catalog,
        graph_runtime=graph_runtime,
        approval_policy=loaded.config.approvals,
        default_branch=loaded.config.default_branch,
        credential_store=credential_store,
        github_client_id=settings.github_client_id,
        github_client_id_source=_github_client_id_source(),
        source_control_preferences=settings.source_control_preferences,
        slack_credential_store=slack_credential_store,
        communications_channel=loaded.config.communications.channel,
        public_url=loaded.config.public_url,
        work_orders=loaded.config.work_orders,
    )


def build_app(config_path: str | os.PathLike[str] | None = None) -> Starlette:
    """Read the configuration and compose the application from it.

    The reloader names this as an import string and calls it with no arguments
    in each child process it starts, so the configuration file is selected by
    ``ENGINE_CONFIG`` there rather than by a command line the child never saw.
    """
    return compose_app(*read_configuration(config_path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine web interface.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    parser.add_argument("--check", action="store_true", help="report wiring and exit")
    args = parser.parse_args(argv)
    try:
        loaded, workflow_catalog = read_configuration(args.config)
    except (EngineConfigError, WorkflowLoadError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    settings = _settings(loaded)

    if args.check:
        report_wiring(settings)
        return 0

    app = compose_app(loaded, workflow_catalog)
    print(describe_loaded_config(loaded))
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
