"""Web control interface entrypoint.

The Python process serves both the chat API and the built assistant-ui client.
``--check`` retains the cheap composition smoke test used in CI.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_review_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.runtime import (
    EngineConfigError,
    LoadedEngineConfig,
    describe_loaded_config,
    load_engine_config,
)

#: Vite's production output, served by the same process as the API.
STATIC_DIRECTORY = Path(__file__).resolve().parents[4] / "dist"


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    review_runners = build_review_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners)
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
    print(
        "review runners: "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in review_runners.items()
        )
    )
    print(f"assistant-ui chat is live; conversations are stored in {settings.sqlite_path}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine web interface.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    parser.add_argument("--check", action="store_true", help="report wiring and exit")
    args = parser.parse_args(argv)
    try:
        loaded = load_engine_config(args.config)
    except EngineConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    settings = Settings(engine_config=loaded.config, config_path=loaded.path)

    if args.check:
        report_wiring(settings)
        return 0

    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    review_runners = build_review_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners)
    app = create_app(
        session,
        runners,
        STATIC_DIRECTORY,
        workflow_runners=workflow_runners,
        review_runners=review_runners,
        approval_policy=loaded.config.approvals,
    )
    print(describe_loaded_config(loaded))
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
