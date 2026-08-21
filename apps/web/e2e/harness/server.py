"""Serve the real web application against scripted provider CLIs.

The browser tier's whole point is that nothing between the click and the
subprocess is a stand-in, so this composes the application exactly as
`engine.apps.web.__main__` does -- same capabilities, same runner mapping, same
approval policy plumbing -- and changes only what a test must own:

    where it works        a fixture repository, so worktrees are disposable
    what it remembers     a SQLite file under the test's own directory
    which CLI it runs     `tests/provider_fakes.py`, scripted per test
    which `gh` it runs    `tests/github_fakes.py`, recording into the state
                          directory instead of commenting on somebody's
                          pull request

Everything else is production wiring, including the parts that are easy to get
wrong: the interactive runners, the write-enabled workflow runners, and the
read-only reviewers that go with them.

Run by `apps/web/e2e/harness.ts`, one process per test, on a port the test
picked. It is not a fixture generator: it starts a server and serves until it
is killed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: apps/web/e2e/harness/server.py -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]

# The fake CLIs are shared with the pytest tier, where they live.
sys.path.insert(0, str(REPO_ROOT / "tests"))

import uvicorn  # noqa: E402

from engine.apps.web.__main__ import STATIC_DIRECTORY  # noqa: E402
from engine.apps.web.api import create_app  # noqa: E402
from engine.apps.web.composition import (  # noqa: E402
    Settings,
    build_capabilities,
    build_review_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.runtime import (  # noqa: E402
    EngineConfigError,
    LoadedEngineConfig,
    describe_loaded_config,
    load_engine_config,
)
import github_fakes  # noqa: E402
from provider_fakes import fake_claude, fake_codex  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--repository",
        required=True,
        help="the git repository conversations and workflow runs work in",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="scratch directory for the database, worktrees, and fake CLIs",
    )
    parser.add_argument(
        "--config",
        help=(
            "an engine.toml for this run. Omitted, the built-in defaults apply "
            "rather than whatever engine.toml the process was started next to."
        ),
    )
    args = parser.parse_args(argv)

    state = Path(args.state)
    binaries = state / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    try:
        loaded = load_engine_config(args.config) if args.config else LoadedEngineConfig()
    except EngineConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if not (STATIC_DIRECTORY / "index.html").is_file():
        print(
            "the assistant-ui client has not been built; "
            "run `npm --prefix apps/web run build`",
            file=sys.stderr,
        )
        return 2

    settings = Settings(
        host="127.0.0.1",
        port=args.port,
        codex_binary=fake_codex(binaries),
        claude_binary=fake_claude(binaries),
        github_binary=github_fakes.install(binaries, state / "gh.jsonl"),
        codex_working_directory=args.repository,
        claude_working_directory=args.repository,
        workspace_root=str(state / "workspaces"),
        sqlite_path=str(state / "conversations.sqlite3"),
        engine_config=loaded.config,
        config_path=loaded.path,
    )
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    review_runners = build_review_runners(settings)
    app = create_app(
        build_session(
            capabilities, runners, args.repository, read_only_runners=review_runners
        ),
        runners,
        STATIC_DIRECTORY,
        workflow_runners=build_workflow_runners(settings),
        review_runners=review_runners,
        approval_policy=loaded.config.approvals,
    )
    print(describe_loaded_config(loaded), flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
