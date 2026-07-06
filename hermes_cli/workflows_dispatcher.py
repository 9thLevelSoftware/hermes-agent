"""SQLite dispatcher for cheap local workflow nodes."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_cli import workflows_db as wfdb
from hermes_cli.workflows_engine import EngineResult, run_in_memory_until_waiting
from hermes_cli.workflows_spec import WorkflowSpec


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _claim_next(
    conn: sqlite3.Connection,
    *,
    now: int,
    lease_seconds: int,
) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT execution_id
          FROM workflow_executions
         WHERE status = 'queued'
           AND (claim_lock IS NULL OR claim_expires <= ?)
         ORDER BY created_at, execution_id
         LIMIT 1
        """,
        (now,),
    ).fetchone()
    if row is None:
        return None

    token = secrets.token_hex(16)
    with wfdb.write_txn(conn):
        updated = conn.execute(
            """
            UPDATE workflow_executions
               SET claim_lock = ?, claim_expires = ?, updated_at = ?
             WHERE execution_id = ?
               AND status = 'queued'
               AND (claim_lock IS NULL OR claim_expires <= ?)
            """,
            (token, now + lease_seconds, now, row["execution_id"], now),
        ).rowcount
    if updated != 1:
        return None
    return row["execution_id"], token


def _append_event(
    conn: sqlite3.Connection,
    execution_id: str,
    kind: str,
    payload: dict[str, Any] | None,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_events (
            execution_id, node_run_id, kind, payload_json, created_at
        ) VALUES (?, NULL, ?, ?, ?)
        """,
        (execution_id, kind, _json_dumps(payload or {}), now),
    )


def _schedule_input(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    spec = wfdb.get_definition(conn, row["workflow_id"], row["version"])
    for trigger in spec.triggers:
        if trigger.type != "schedule":
            continue
        expr = trigger.cron or trigger.schedule or getattr(trigger, "expr", None)
        if row["trigger_id"] is not None:
            if trigger.id == row["trigger_id"]:
                return dict(trigger.input)
        elif expr == row["schedule"]:
            return dict(trigger.input)
    return {}


def _fire_due_schedules(conn: sqlite3.Connection, *, now: int) -> None:
    with wfdb.write_txn(conn):
        rows = conn.execute(
            """
            SELECT * FROM workflow_schedules
             WHERE enabled = 1
               AND next_run_at IS NOT NULL
               AND next_run_at <= ?
             ORDER BY next_run_at, id
            """,
            (now,),
        ).fetchall()
        for row in rows:
            wfdb.start_execution(
                conn,
                row["workflow_id"],
                input_data=_schedule_input(conn, row),
                trigger_type="schedule",
                trigger_id=row["trigger_id"],
                version=row["version"],
                now=now,
            )
            conn.execute(
                """
                UPDATE workflow_schedules
                   SET next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (wfdb._next_cron_run(row["schedule"], now), now, row["id"]),
            )


def _resume_due_waits(conn: sqlite3.Connection, *, now: int) -> None:
    with wfdb.write_txn(conn):
        rows = conn.execute(
            """
            SELECT nr.id, nr.execution_id
              FROM workflow_node_runs nr
              JOIN workflow_executions ex ON ex.execution_id = nr.execution_id
             WHERE nr.status = 'waiting'
               AND nr.wait_until IS NOT NULL
               AND nr.wait_until <= ?
               AND ex.status = 'waiting'
             ORDER BY nr.wait_until, nr.id
            """,
            (now,),
        ).fetchall()
        for row in rows:
            updated = conn.execute(
                """
                UPDATE workflow_node_runs
                   SET status = 'succeeded', completed_at = ?
                 WHERE id = ? AND status = 'waiting'
                """,
                (now, row["id"]),
            ).rowcount
            if updated:
                conn.execute(
                    """
                    UPDATE workflow_executions
                       SET status = 'queued', claim_lock = NULL,
                           claim_expires = NULL, updated_at = ?
                     WHERE execution_id = ? AND status = 'waiting'
                    """,
                    (now, row["execution_id"]),
                )


def _completed_wait_nodes(conn: sqlite3.Connection, execution_id: str) -> set[str]:
    return {
        row["node_id"]
        for row in conn.execute(
            """
            SELECT node_id FROM workflow_node_runs
             WHERE execution_id = ?
               AND status = 'succeeded'
               AND wait_until IS NOT NULL
            """,
            (execution_id,),
        )
    }


def _persist_waiting_nodes(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
) -> None:
    if result.status != "waiting":
        return
    for node_id in result.waiting_nodes:
        node = spec.nodes.get(node_id) if spec is not None else None
        if node is not None and node.type == "wait":
            wait_until = now + node.seconds
        else:
            wait_until = None
        exists = conn.execute(
            """
            SELECT 1 FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = ? AND status = 'waiting'
            """,
            (execution_id, node_id),
        ).fetchone()
        if exists is None:
            conn.execute(
                """
                INSERT INTO workflow_node_runs (
                    execution_id, node_id, status, started_at, wait_until
                ) VALUES (?, ?, 'waiting', ?, ?)
                """,
                (execution_id, node_id, now, wait_until),
            )


def _finish(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    token: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
) -> bool:
    final_event = {
        "succeeded": "execution_succeeded",
        "waiting": "execution_waiting",
        "failed": "execution_failed",
    }[result.status]
    final_payload: dict[str, Any] = {}
    if result.status == "waiting":
        final_payload = {"waiting_nodes": result.waiting_nodes}
    elif result.status == "failed":
        final_payload = {"error": result.error or {}}

    with wfdb.write_txn(conn):
        row = conn.execute(
            "SELECT claim_lock FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None or row["claim_lock"] != token:
            return False

        existing_events = conn.execute(
            "SELECT kind, payload_json FROM workflow_events WHERE execution_id = ?",
            (execution_id,),
        ).fetchall()
        emitted_nodes: set[str] = set()
        for event in existing_events:
            if event["kind"] != "node_succeeded":
                continue
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, ValueError):
                continue
            node_id = payload.get("node_id")
            if isinstance(node_id, str):
                emitted_nodes.add(node_id)

        _persist_waiting_nodes(
            conn,
            execution_id=execution_id,
            result=result,
            spec=spec,
            now=now,
        )
        if not existing_events:
            _append_event(conn, execution_id, "execution_started", {}, now)
        for node_id, node_context in result.context.get("node", {}).items():
            if node_id in emitted_nodes:
                continue
            if isinstance(node_context, dict):
                output = node_context.get("output")
            else:
                output = None
            _append_event(
                conn,
                execution_id,
                "node_succeeded",
                {"node_id": node_id, "output": output},
                now,
            )
            emitted_nodes.add(node_id)
        _append_event(conn, execution_id, final_event, final_payload, now)
        conn.execute(
            """
            UPDATE workflow_executions
               SET status = ?, context_json = ?, claim_lock = NULL,
                   claim_expires = NULL, updated_at = ?
             WHERE execution_id = ? AND claim_lock = ?
            """,
            (result.status, _json_dumps(result.context), now, execution_id, token),
        )
    return True


def tick(
    *,
    db_path: Path | None = None,
    limit: int = 10,
    now: int | None = None,
    lease_seconds: int = 60,
) -> int:
    """Advance up to limit queued cheap workflow executions. Return number processed."""
    if limit <= 0:
        return 0

    tick_now = int(time.time()) if now is None else now
    processed = 0
    with wfdb.connect(db_path) as conn:
        _fire_due_schedules(conn, now=tick_now)
        _resume_due_waits(conn, now=tick_now)
        while processed < limit:
            claimed = _claim_next(conn, now=tick_now, lease_seconds=lease_seconds)
            if claimed is None:
                break
            execution_id, token = claimed
            execution = None
            spec = None
            try:
                execution = wfdb.get_execution(conn, execution_id)
                spec = wfdb.get_definition(conn, execution.workflow_id, execution.version)
                completed_wait_nodes = _completed_wait_nodes(conn, execution_id)
                if completed_wait_nodes:
                    result = run_in_memory_until_waiting(
                        spec,
                        execution.input,
                        completed_wait_nodes=completed_wait_nodes,
                    )
                else:
                    result = run_in_memory_until_waiting(spec, execution.input)
            except Exception as exc:
                context = execution.context if execution is not None else {"node": {}}
                result = EngineResult(
                    status="failed",
                    context=context,
                    waiting_nodes=[],
                    error={"message": str(exc)},
                )
            if _finish(
                conn,
                execution_id=execution_id,
                token=token,
                result=result,
                spec=spec,
                now=tick_now,
            ):
                processed += 1
    return processed
