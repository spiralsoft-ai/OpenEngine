"""Worker entrypoint.

Ticket 1 stops at composition: it builds the dispatcher and reports it. Polling
a Temporal task queue lands with the workflow ticket.
"""

import argparse
import sys
from collections.abc import Sequence

from engine.apps.worker.composition import Settings, build_capabilities, build_dispatcher
from engine.runtime import EngineConfigError, describe_loaded_config, load_engine_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine worker.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    args = parser.parse_args(argv)
    try:
        loaded = load_engine_config(args.config)
    except EngineConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    settings = Settings(engine_config=loaded.config, config_path=loaded.path)
    capabilities = build_capabilities(settings)
    build_dispatcher(settings)
    print(describe_loaded_config(loaded))
    print(f"engine worker -- task queue {settings.task_queue!r}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print("no task-queue polling yet; see Ticket 1 acceptance criteria.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
