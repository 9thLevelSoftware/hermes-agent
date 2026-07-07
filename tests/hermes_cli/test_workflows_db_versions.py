import json
from argparse import Namespace

import pytest

from hermes_cli import workflows
from hermes_cli import workflows_db as wfdb
from hermes_cli.workflows_spec import WorkflowSpec


def _spec(output=True, *, version=1):
    return WorkflowSpec.model_validate({
        "id": "immutable_demo",
        "name": "Immutable Demo",
        "version": version,
        "nodes": {"start": {"type": "pass", "output": {"ok": output}}},
    })


def _schedule_spec(*, version=1):
    return WorkflowSpec.model_validate({
        "id": "immutable_schedule_demo",
        "name": "Immutable Schedule Demo",
        "version": version,
        "triggers": [{"type": "schedule", "id": "hourly", "cron": "0 * * * *"}],
        "nodes": {"start": {"type": "pass", "output": {"ok": True}}},
    })


def test_redeploy_same_version_same_checksum_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _spec(True), created_by="first")
        first = wfdb.list_definitions(conn)[0]
        wfdb.deploy_definition(conn, _spec(True), created_by="second")
        second = wfdb.list_definitions(conn)[0]

    assert first.workflow_id == "immutable_demo"
    assert first.version == 1
    assert second.workflow_id == first.workflow_id
    assert second.version == first.version
    assert second.checksum == first.checksum
    assert second.created_by == "first", "idempotent redeploy must not overwrite created_by"
    assert second.created_at == first.created_at, "idempotent redeploy must not bump created_at"
    assert second.name == first.name
    assert second.enabled == first.enabled
    assert second.spec.model_dump(mode="json") == first.spec.model_dump(mode="json")


def test_redeploy_same_version_different_checksum_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _spec(True), created_by="first")
        with pytest.raises(ValueError) as exc:
            wfdb.deploy_definition(conn, _spec(False), created_by="second")

    assert "already exists with different checksum" in str(exc.value)


def test_deploy_new_version_with_different_checksum_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _spec(True, version=1), created_by="v1")
        wfdb.deploy_definition(conn, _spec(False, version=2), created_by="v2")
        records = wfdb.list_definitions(conn)

    assert {(r.workflow_id, r.version) for r in records} == {("immutable_demo", 1), ("immutable_demo", 2)}


def test_cli_deploy_json_reports_exact_deployed_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    v1 = tmp_path / "v1.json"
    v2 = tmp_path / "v2.json"
    v1.write_text(json.dumps(_spec(True, version=1).model_dump(mode="json")), encoding="utf-8")
    v2.write_text(json.dumps(_spec(False, version=2).model_dump(mode="json")), encoding="utf-8")

    assert workflows._cmd_deploy(Namespace(file=str(v2), json=True)) == 0
    capsys.readouterr()
    assert workflows._cmd_deploy(Namespace(file=str(v1), json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow_id"] == "immutable_demo"
    assert payload["version"] == 1


def test_redeploy_same_schedule_definition_preserves_schedule_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _schedule_spec(), created_by="first")
        first_rows = conn.execute(
            "SELECT id, workflow_id, version, trigger_id, schedule, enabled, "
            "next_run_at, created_at, updated_at FROM workflow_schedules"
        ).fetchall()
        first_row = dict(first_rows[0])

        wfdb.deploy_definition(conn, _schedule_spec(), created_by="second")
        second_rows = conn.execute(
            "SELECT id, workflow_id, version, trigger_id, schedule, enabled, "
            "next_run_at, created_at, updated_at FROM workflow_schedules"
        ).fetchall()

    assert len(first_rows) == 1
    assert len(second_rows) == 1
    second_row = dict(second_rows[0])
    assert second_row == first_row, (
        f"schedule row mutated on idempotent redeploy: {first_row} -> {second_row}"
    )