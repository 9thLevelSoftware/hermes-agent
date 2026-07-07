from __future__ import annotations

import subprocess


def _make_task(
    kb,
    *,
    assignee: str,
    provider_override: str | None = None,
    model_override: str | None = None,
):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        provider_override=provider_override,
        model_override=model_override,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_default_spawn_passes_provider_and_model_overrides(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    pid = kb._default_spawn(
        _make_task(
            kb,
            assignee="reviewer",
            provider_override="openai-codex",
            model_override="gpt-5.5",
        ),
        str(workspace),
    )

    assert pid == 4243
    assert captured["cmd"][:3] == ["hermes", "-p", "reviewer"]

    cmd = captured["cmd"]
    chat_index = cmd.index("chat")
    provider_index = cmd.index("--provider")
    model_index = cmd.index("-m")
    assert provider_index > chat_index
    assert model_index > chat_index
    assert cmd[provider_index + 1] == "openai-codex"
    assert cmd[model_index + 1] == "gpt-5.5"

    from hermes_cli._parser import build_top_level_parser

    parser, _, _ = build_top_level_parser()
    parse_argv = cmd[1:]
    profile_index = parse_argv.index("-p")
    parse_argv = parse_argv[:profile_index] + parse_argv[profile_index + 2 :]
    parsed = parser.parse_args(parse_argv)
    assert parsed.provider == "openai-codex"
    assert parsed.model == "gpt-5.5"
    if "--toolsets" in cmd:
        assert cmd.index("--toolsets") > chat_index
        assert parsed.toolsets == cmd[cmd.index("--toolsets") + 1]
