import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import workflows_db as wfdb
from hermes_cli import workflows_dispatcher
from hermes_cli.workflows_spec import WorkflowSpec


def _approval_workflow() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "approval_e2e",
        "name": "Approval E2E",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "implement": {
                "type": "agent_task",
                "profile": "worker",
                "title": "Implement change",
                "prompt": {
                    "subject": "${ input.subject }",
                    "request": "return verdict JSON",
                },
                "workspace_kind": "scratch",
            },
            "route": {
                "type": "switch",
                "cases": [{
                    "name": "approved",
                    "when": {
                        "op": "eq",
                        "left": {"path": "$.node.implement.output.verdict"},
                        "right": "approved",
                    },
                }],
            },
            "approved": {
                "type": "pass",
                "output": {
                    "verdict": "${ node.implement.output.verdict }",
                    "path": "approved",
                },
            },
            "revise": {
                "type": "agent_task",
                "profile": "worker",
                "title": "Revise change",
                "prompt": {"reason": "${ node.implement.output.reason }"},
                "workspace_kind": "scratch",
            },
        },
        "edges": [
            {"from": "implement", "to": "route"},
            {"from": "route.approved", "to": "approved"},
            {"from": "route.default", "to": "revise"},
        ],
    })


def _workflow_events(exec_id: str) -> list[dict]:
    with wfdb.connect() as conn:
        return [dict(row) for row in conn.execute(
            """
            SELECT id, kind, payload_json, created_at
              FROM workflow_events
             WHERE execution_id = ?
             ORDER BY id
            """,
            (exec_id,),
        )]


def test_workflow_kanban_completion_routes_approved_path_e2e(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()

    spec = _approval_workflow()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        exec_id = wfdb.start_execution(
            conn,
            spec.id,
            input_data={"subject": "deadlift form"},
            trigger_type="manual",
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with wfdb.connect() as conn, kb.connect() as kconn:
        execution = wfdb.get_execution(conn, exec_id)
        tasks = kb.list_tasks(kconn)
        waiting_run = conn.execute(
            """
            SELECT node_id, status, kanban_task_id
              FROM workflow_node_runs
             WHERE execution_id = ?
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "waiting"
    assert len(tasks) == 1
    task = tasks[0]
    assert task.workflow_template_id == spec.id
    assert task.current_step_key == "implement"
    assert task.created_by == f"workflow:{exec_id}"
    assert task.assignee == "worker"
    assert task.status in {"ready", "todo"}
    assert task.body is not None and "deadlift form" in task.body
    assert dict(waiting_run) == {
        "node_id": "implement",
        "status": "waiting",
        "kanban_task_id": task.id,
    }

    with kb.connect() as kconn:
        assert kb.complete_task(kconn, task.id, result=json.dumps({"verdict": "approved"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn, kb.connect() as kconn:
        execution = wfdb.get_execution(conn, exec_id)
        tasks = kb.list_tasks(kconn, workflow_template_id=spec.id)
        runs = [dict(row) for row in conn.execute(
            """
            SELECT node_id, status, output_json, kanban_task_id
              FROM workflow_node_runs
             WHERE execution_id = ?
             ORDER BY id
            """,
            (exec_id,),
        )]
    assert execution.status == "succeeded"
    assert execution.context["node"]["implement"]["output"] == {"verdict": "approved"}
    assert execution.context["node"]["approved"]["output"] == {
        "verdict": "approved",
        "path": "approved",
    }
    assert [task.current_step_key for task in tasks] == ["implement"]
    assert len(runs) == 1
    assert {key: runs[0][key] for key in ("node_id", "status", "kanban_task_id")} == {
        "node_id": "implement",
        "status": "succeeded",
        "kanban_task_id": task.id,
    }
    assert json.loads(runs[0]["output_json"]) == {"verdict": "approved"}

    events = _workflow_events(exec_id)
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert [event["created_at"] for event in events] == [100, 100, 101, 101, 101]
    assert [event["kind"] for event in events] == [
        "execution_started",
        "execution_waiting",
        "node_succeeded",
        "node_succeeded",
        "execution_succeeded",
    ]
    assert [
        json.loads(event["payload_json"]).get("node_id")
        for event in events
        if event["kind"] == "node_succeeded"
    ] == ["implement", "approved"]
