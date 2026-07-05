import json

from hermes_cli import workflows_db as wfdb
from hermes_cli import workflows_dispatcher
from hermes_cli.workflows_engine import EngineResult
from hermes_cli.workflows_spec import WorkflowSpec


def _switch_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "demo", "name": "Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {"type": "pass", "output": {"score": "${ input.score }"}},
            "route": {"type": "switch", "cases": [
                {"name": "high", "when": {"op": "gte", "left": {"path": "$.node.start.output.score"}, "right": 0.8}}
            ]},
            "high": {"type": "pass", "output": {"bucket": "high"}},
            "low": {"type": "pass", "output": {"bucket": "low"}},
        },
        "edges": [
            {"from": "start", "to": "route"},
            {"from": "route.high", "to": "high"},
            {"from": "route.default", "to": "low"},
        ],
    })


def _start_execution(tmp_path, monkeypatch, input_data=None) -> str:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        return wfdb.start_execution(
            conn,
            "demo",
            input_data={} if input_data is None else input_data,
            trigger_type="manual",
        )


def _execution_state(exec_id: str):
    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        claim = dict(conn.execute(
            """
            SELECT claim_lock, claim_expires
              FROM workflow_executions
             WHERE execution_id = ?
            """,
            (exec_id,),
        ).fetchone())
        events = [dict(row) for row in conn.execute(
            """
            SELECT kind, payload_json
              FROM workflow_events
             WHERE execution_id = ?
             ORDER BY id
            """,
            (exec_id,),
        )]
    return execution, claim, events


def test_tick_runs_queued_pass_switch_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        exec_id = wfdb.start_execution(conn, "demo", input_data={"score": 0.9}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        events = [row["kind"] for row in conn.execute(
            "SELECT kind FROM workflow_events WHERE execution_id = ? ORDER BY id",
            (exec_id,),
        )]
    assert execution.status == "succeeded"
    assert execution.context["node"]["high"]["output"] == {"bucket": "high"}
    assert "execution_started" in events
    assert "node_succeeded" in events
    assert "execution_succeeded" in events


def test_tick_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        first = wfdb.start_execution(conn, "demo", input_data={"score": 0.9}, trigger_type="manual")
        second = wfdb.start_execution(conn, "demo", input_data={"score": 0.1}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1) == 1

    with wfdb.connect() as conn:
        statuses = {
            exec_id: wfdb.get_execution(conn, exec_id).status
            for exec_id in (first, second)
        }
    assert sorted(statuses.values()) == ["queued", "succeeded"]


def test_waiting_result_persists_and_is_not_retried(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    calls = []

    def waiting_result(spec, input_data):
        calls.append((spec.id, input_data))
        return EngineResult(
            status="waiting",
            context={"input": {}, "node": {}},
            waiting_nodes=["pause"],
        )

    monkeypatch.setattr(
        workflows_dispatcher, "run_in_memory_until_waiting", waiting_result
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "waiting"
    assert execution.context == {"input": {}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], json.loads(event["payload_json"])) for event in events] == [
        ("execution_started", {}),
        ("execution_waiting", {"waiting_nodes": ["pause"]}),
    ]

    assert workflows_dispatcher.tick(limit=1, now=101) == 0
    assert len(calls) == 1
    assert _execution_state(exec_id)[2] == events


def test_failed_result_persists_deterministic_error_payload(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: EngineResult(
            status="failed",
            context={"input": {}, "node": {}},
            waiting_nodes=[],
            error={"message": "boom"},
        ),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "failed"
    assert execution.context == {"input": {}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], event["payload_json"]) for event in events] == [
        ("execution_started", "{}"),
        ("execution_failed", '{"error":{"message":"boom"}}'),
    ]


def test_engine_exception_persists_failed_and_clears_claim(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch, {"score": 0.9})

    def raise_boom(spec, input_data):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        workflows_dispatcher, "run_in_memory_until_waiting", raise_boom
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "failed"
    assert execution.context == {"input": {"score": 0.9}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], json.loads(event["payload_json"])) for event in events] == [
        ("execution_started", {}),
        ("execution_failed", {"error": {"message": "boom"}}),
    ]


def test_non_expired_claim_is_skipped_and_expired_claim_is_reclaimed(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: calls.append(input_data) or EngineResult(
            status="succeeded",
            context={"input": {}, "node": {}},
            waiting_nodes=[],
        ),
    )

    with wfdb.connect() as conn:
        conn.execute(
            """
            UPDATE workflow_executions
               SET claim_lock = 'busy', claim_expires = 200
             WHERE execution_id = ?
            """,
            (exec_id,),
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 0
    assert calls == []
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "queued"
    assert claim == {"claim_lock": "busy", "claim_expires": 200}
    assert events == []

    with wfdb.connect() as conn:
        conn.execute(
            """
            UPDATE workflow_executions
               SET claim_expires = 99
             WHERE execution_id = ?
            """,
            (exec_id,),
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "execution_succeeded",
    ]
    assert len(calls) == 1


def test_repeated_tick_after_final_status_does_not_duplicate_events(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: calls.append(input_data) or EngineResult(
            status="succeeded",
            context={"input": {}, "node": {"start": {"output": {"ok": True}}}},
            waiting_nodes=[],
        ),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "node_succeeded",
        "execution_succeeded",
    ]
    assert len(calls) == 1
