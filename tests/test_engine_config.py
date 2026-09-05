"""Engine's TOML configuration is singular and typed."""

from pathlib import Path

import pytest

import engine.apps.control_server.__main__ as control_server_main
import engine.apps.web.__main__ as web_main
import engine.apps.worker.__main__ as worker_main
from engine.runtime import (
    ApprovalCapability,
    EngineConfigError,
    ResponseStyle,
    describe_loaded_config,
    load_engine_config,
    parse_engine_config,
)


def test_defaults_allow_reads_without_selecting_a_file(tmp_path: Path) -> None:
    loaded = load_engine_config(environ={}, cwd=tmp_path)

    assert loaded.path is None
    assert loaded.config.attribution is True
    assert loaded.config.default_branch == "main"
    assert loaded.config.public_url == ""
    assert loaded.config.communications.provider == "slack"
    assert loaded.config.communications.channel == ""
    assert loaded.config.work_orders.repository == ""
    assert loaded.config.work_orders.workflow == ""
    assert loaded.config.work_orders.runner == ""
    assert loaded.config.orchestrator.host == "127.0.0.1:7233"
    assert loaded.config.orchestrator.database == ".engine/temporal.sqlite3"
    assert loaded.config.orchestrator.health_check_interval == 5.0
    assert loaded.config.claude.output_style is None
    assert loaded.config.approvals.allow == (ApprovalCapability.READ,)
    assert loaded.config.approvals.auto_approve is False
    assert loaded.config.approvals.bash.allow == ()


def test_loads_provider_neutral_approval_configuration(tmp_path: Path) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text(
        """
[approvals]
auto_approve = true
allow = ["read", "edit"]

[approvals.bash]
allow = ["uv run pytest **", "git status **"]
ask = ["git push **"]
deny = ["sudo **"]
""".strip()
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.path == path.resolve()
    assert loaded.config.attribution is True
    assert loaded.config.approvals.auto_approve is True
    assert loaded.config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.EDIT,
    )
    assert loaded.config.approvals.bash.allow == (
        "uv run pytest **",
        "git status **",
    )
    assert loaded.config.approvals.bash.ask == ("git push **",)
    assert loaded.config.approvals.bash.deny == ("sudo **",)


def test_loads_a_configured_default_branch(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('default_branch = "master"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.default_branch == "master"


def test_loads_communications_notification_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'public_url = "https://engine.example/"\n'
        '[communications]\nprovider = "slack"\nchannel = "C12345678"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.public_url == "https://engine.example"
    assert loaded.config.communications.channel == "C12345678"


def test_rejects_legacy_slack_channel_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[slack]\nchannel_id = "C12345678"\n')

    with pytest.raises(EngineConfigError, match="unknown key in configuration: slack"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_communications_provider(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[communications]\nprovider = "buzz"\nchannel = "engineering"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.communications.provider == "buzz"
    assert loaded.config.communications.channel == "engineering"


def test_loads_what_a_mention_should_start(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        "[work_orders]\n"
        'repository = "acme/api"\n'
        'workflow = "implementation-review-v1"\n'
        'runner = "claude"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.work_orders.repository == "acme/api"
    assert loaded.config.work_orders.workflow == "implementation-review-v1"
    assert loaded.config.work_orders.runner == "claude"


def test_rejects_an_unknown_work_order_key(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[work_orders]\nrepo = "acme/api"\n')

    with pytest.raises(EngineConfigError, match="unknown key in work_orders: repo"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_github_deployment_credentials(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.github_client_id == "client-from-toml"
    assert loaded.config.github_token == "token-from-toml"


def test_selection_is_explicit_then_environment_then_working_directory(
    tmp_path: Path,
) -> None:
    implicit = tmp_path / "engine.toml"
    environment = tmp_path / "environment.toml"
    explicit = tmp_path / "explicit.toml"
    implicit.write_text('[approvals]\nallow = ["read"]\n')
    environment.write_text('[approvals]\nallow = ["edit"]\n')
    explicit.write_text('[approvals]\nallow = ["web"]\n')

    from_default = load_engine_config(environ={}, cwd=tmp_path)
    from_environment = load_engine_config(
        environ={"ENGINE_CONFIG": environment.name}, cwd=tmp_path
    )
    from_explicit = load_engine_config(
        explicit.name,
        environ={"ENGINE_CONFIG": environment.name},
        cwd=tmp_path,
    )

    assert from_default.path == implicit.resolve()
    assert from_default.config.approvals.allow == (ApprovalCapability.READ,)
    assert from_environment.path == environment.resolve()
    assert from_environment.config.approvals.allow == (ApprovalCapability.EDIT,)
    assert from_explicit.path == explicit.resolve()
    assert from_explicit.config.approvals.allow == (ApprovalCapability.WEB,)


@pytest.mark.parametrize(
    "document,message",
    [
        ({"approval": {}}, "unknown key in configuration: approval"),
        ({"approvals": {"automatic": True}}, "unknown key in approvals: automatic"),
        ({"approvals": {"auto_approve": "yes"}}, "must be a boolean"),
        ({"attribution": "no"}, "attribution must be a boolean"),
        ({"default_branch": ""}, "default_branch must be a non-empty string"),
        ({"default_branch": 1}, "default_branch must be a non-empty string"),
        ({"github_client_id": 1}, "github_client_id must be a string"),
        ({"github_token": " "}, "github_token must not be blank"),
        ({"orchestrator": {"host": ""}}, "orchestrator.host must not be blank"),
        (
            {"orchestrator": {"health_check_interval": 0}},
            "orchestrator.health_check_interval must be a positive number",
        ),
        ({"output_style": "concise"}, "unknown key in configuration: output_style"),
        ({"claude": {"style": "concise"}}, "unknown key in claude: style"),
        ({"claude": {"output_style": True}}, "claude.output_style must be a string"),
        (
            {"claude": {"output_style": "Concise"}},
            "claude.output_style is unknown: 'Concise'",
        ),
        (
            {"claude": {"output_style": "terse"}},
            "expected one of: concise, explanatory, learning",
        ),
        ({"approvals": {"allow": "read"}}, "must be an array of strings"),
        ({"approvals": {"allow": ["Read"]}}, "unknown capability 'Read'"),
        ({"approvals": {"allow": ["read", "read"]}}, "must not contain duplicates"),
        (
            {"approvals": {"bash": {"allow": [" "]}}},
            "must not contain empty patterns",
        ),
        (
            {"approvals": {"bash": {"allowed": ["pytest **"]}}},
            "unknown key in approvals.bash: allowed",
        ),
    ],
)
def test_rejects_mistyped_or_unknown_settings(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config(document)


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EngineConfigError, match="configuration file does not exist"):
        load_engine_config("missing.toml", environ={}, cwd=tmp_path)


def test_attribution_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text("attribution = false\n")

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.attribution is False


def test_output_style_is_engine_vocabulary_scoped_to_the_runner_that_honours_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[claude]\noutput_style = "concise"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.claude.output_style is ResponseStyle.CONCISE


def test_invalid_toml_names_its_source(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[approvals\n")

    with pytest.raises(EngineConfigError, match=f"invalid TOML in {path}"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_startup_description_reports_the_policy_being_enforced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        '[approvals]\nauto_approve = true\nallow = ["read", "mcp"]\n'
        '[approvals.bash]\nallow = ["pytest **"]\nask = ["git push **"]\n'
    )

    description = describe_loaded_config(load_engine_config(path, environ={}))

    assert str(path) in description
    assert "auto_approve=on" in description
    assert "allow=read, mcp" in description
    assert "bash_rules=2" in description
    assert "approvals enforced" in description
    assert "claude.output_style=provider default" in description
    assert "default_branch=main" in description


def test_web_entrypoint_puts_explicit_config_in_composition_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text('[approvals]\nauto_approve = true\nallow = ["read", "bash"]\n')
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert len(seen) == 1
    assert seen[0].config_path == path.resolve()
    assert seen[0].engine_config.approvals.auto_approve is True
    assert seen[0].engine_config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.BASH,
    )


def test_web_entrypoint_uses_toml_github_credentials_when_environment_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-toml"
    assert seen[0].github_token == "token-from-toml"


def test_web_entrypoint_prefers_environment_github_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-from-environment")
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-environment")
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-environment"
    assert seen[0].github_token == "token-from-environment"


@pytest.mark.parametrize(
    "entrypoint,arguments",
    [
        (web_main.main, ["--check"]),
        (worker_main.main, []),
        (control_server_main.main, []),
    ],
)
def test_every_entrypoint_rejects_an_explicit_missing_config(
    entrypoint, arguments: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.toml"

    assert entrypoint([*arguments, "--config", str(missing)]) == 2

    assert "configuration file does not exist" in capsys.readouterr().err
