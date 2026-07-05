from pathlib import Path

from hermes_cli import workflows_db as wfdb
from hermes_cli.workflows_spec import WorkflowSpec


def test_init_db_creates_tables(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    wfdb.init_db()
    with wfdb.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "workflow_definitions" in tables
    assert "workflow_executions" in tables
    assert "workflow_node_runs" in tables
    assert "workflow_events" in tables
    assert "workflow_schedules" in tables


def test_deploy_definition_and_get_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = WorkflowSpec.model_validate({
        "id": "demo", "name": "Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    })
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        loaded = wfdb.get_definition(conn, "demo")
    assert loaded.id == "demo"
    assert loaded.version == 1


def test_start_execution_persists_input_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "demo", "name": "Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        exec_id = wfdb.start_execution(conn, "demo", input_data={"x": 1}, trigger_type="manual")
        execution = wfdb.get_execution(conn, exec_id)
    assert execution.workflow_id == "demo"
    assert execution.status == "queued"
    assert execution.input == {"x": 1}
    assert execution.context == {"input": {"x": 1}, "node": {}}


def test_list_definitions_and_append_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = WorkflowSpec.model_validate({
        "id": "demo", "name": "Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    })
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        records = wfdb.list_definitions(conn)
        exec_id = wfdb.start_execution(conn, "demo", input_data={"x": 1}, trigger_type="manual")
        wfdb.append_event(conn, exec_id, "queued", {"b": 2, "a": 1})
        event = conn.execute(
            "SELECT kind, payload_json FROM workflow_events WHERE execution_id = ?",
            (exec_id,),
        ).fetchone()
    assert [record.workflow_id for record in records] == ["demo"]
    assert records[0].spec.id == "demo"
    assert event["kind"] == "queued"
    assert event["payload_json"] == '{"a":1,"b":2}'
