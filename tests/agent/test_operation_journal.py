"""Tests for the shared durable operation journal."""

import json
import sqlite3
import time
from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from agent.operation_journal import OperationJournal, OperationRecord
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _journal(db):
    return OperationJournal(db)


def test_schema_and_profile_isolation(tmp_path):
    first = SessionDB(db_path=tmp_path / "first" / "state.db")
    second = SessionDB(db_path=tmp_path / "second" / "state.db")
    try:
        first_journal = _journal(first)
        second_journal = _journal(second)

        record = first_journal.create(operation_id="shared-id", kind="tool")

        assert isinstance(record, OperationRecord)
        assert is_dataclass(record)
        assert second_journal.get("shared-id") is None
        assert first_journal.list_unacknowledged() == [record]

        columns = [
            row[1]
            for row in first._conn.execute("PRAGMA table_info(agent_operations)")
        ]
        assert columns == [
            "operation_id",
            "kind",
            "session_id",
            "turn_id",
            "tool_call_id",
            "destination",
            "payload_hash",
            "state",
            "effect_disposition",
            "result_json",
            "error",
            "created_at",
            "updated_at",
            "acknowledged_at",
        ]
        indexes = {
            tuple(row[2] for row in first._conn.execute(
                f'PRAGMA index_info("{index_name}")'
            ))
            for index_name in (
                "idx_agent_operations_kind_state_updated",
                "idx_agent_operations_session_updated",
            )
        }
        assert indexes == {
            ("kind", "state", "updated_at"),
            ("session_id", "updated_at"),
        }
    finally:
        first.close()
        second.close()


def test_create_defaults_and_idempotent_identity(db):
    journal = _journal(db)

    created = journal.create(
        operation_id="op-1",
        kind="tool",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        destination="telegram:chat-1",
        payload_hash="hash-1",
    )
    same = journal.create(
        operation_id="op-1",
        kind="tool",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        destination="telegram:chat-1",
        payload_hash="hash-1",
    )

    assert created == same
    assert created.state == "pending"
    assert created.effect_disposition == "none"
    assert created.result_json is None
    assert created.error is None
    assert created.acknowledged_at is None
    with pytest.raises(FrozenInstanceError):
        created.state = "running"


def test_create_rejects_conflicting_identity(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool", destination="one")

    with pytest.raises(ValueError):
        journal.create(operation_id="op-1", kind="tool", destination="two")

    assert journal.get("op-1").destination == "one"


def test_valid_transition_serializes_result_and_invalid_or_stale_leaves_row_unchanged(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="confirmed",
            effect_disposition="landed",
        )
    original = journal.get("op-1")
    assert original.state == "pending"

    running = journal.transition(
        "op-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    assert running.state == "running"

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="failed",
            effect_disposition="unknown",
        )
    assert journal.get("op-1") == running

    result = journal.transition(
        "op-1",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="landed",
        result={"z": 1, "a": "value"},
    )
    assert result.result_json == json.dumps(
        {"z": 1, "a": "value"}, sort_keys=True, default=str
    )

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"confirmed"},
            to_state="running",
            effect_disposition="none",
        )
    assert journal.get("op-1") == result


def test_transition_rejects_invalid_state_and_effect(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="not-a-state",
            effect_disposition="none",
        )
    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="running",
            effect_disposition="not-an-effect",
        )
    assert journal.get("op-1").state == "pending"


def test_reconcile_marks_inflight_unknown_without_retrying(db):
    journal = _journal(db)
    journal.create(operation_id="pending", kind="tool")
    journal.create(operation_id="running", kind="tool")
    journal.create(operation_id="dispatched", kind="tool")
    journal.create(operation_id="confirmed", kind="tool")
    journal.create(operation_id="failed", kind="tool")
    journal.create(operation_id="unknown", kind="tool")
    journal.create(operation_id="cancelled", kind="tool")

    journal.transition(
        "running",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "dispatched",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "dispatched",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="none",
    )
    journal.transition(
        "confirmed",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "confirmed",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="landed",
    )
    journal.transition(
        "failed",
        from_states={"pending"},
        to_state="failed",
        effect_disposition="unknown",
    )
    journal.transition(
        "unknown",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "unknown",
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
    )
    journal.transition(
        "cancelled",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )

    assert journal.reconcile_after_restart() == 2
    assert journal.get("pending").state == "pending"
    assert journal.get("running").state == "unknown"
    assert journal.get("running").effect_disposition == "unknown"
    assert journal.get("dispatched").state == "unknown"
    assert journal.get("dispatched").effect_disposition == "unknown"
    assert journal.get("confirmed").state == "confirmed"
    assert journal.get("failed").state == "failed"
    assert journal.get("unknown").state == "unknown"
    assert journal.get("cancelled").state == "cancelled"

    with pytest.raises(ValueError):
        journal.transition(
            "running",
            from_states={"unknown"},
            to_state="running",
            effect_disposition="none",
        )


def test_acknowledge_only_terminal_and_is_idempotent(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")
    assert journal.acknowledge("op-1") is False

    journal.transition(
        "op-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    assert journal.acknowledge("op-1") is False
    journal.transition(
        "op-1",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="none",
    )
    assert journal.acknowledge("op-1") is False
    journal.transition(
        "op-1",
        from_states={"dispatched"},
        to_state="failed",
        effect_disposition="unknown",
        error="send failed",
    )

    assert journal.acknowledge("op-1") is True
    assert journal.acknowledge("op-1") is False
    assert journal.get("op-1").acknowledged_at is not None


def test_list_unacknowledged_filters_kind_and_orders_created_at(db):
    journal = _journal(db)
    journal.create(operation_id="first", kind="tool")
    time.sleep(0.002)
    journal.create(operation_id="second", kind="delivery")
    time.sleep(0.002)
    journal.create(operation_id="third", kind="tool")
    journal.transition(
        "third",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )
    assert journal.acknowledge("third") is True

    assert [r.operation_id for r in journal.list_unacknowledged()] == [
        "first",
        "second",
    ]
    assert [r.operation_id for r in journal.list_unacknowledged(kind="tool")] == [
        "first",
    ]


def test_database_checks_states_and_effects(db):
    with pytest.raises(sqlite3.IntegrityError):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO agent_operations "
                "(operation_id, kind, state, effect_disposition, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("bad-state", "tool", "invalid", "none", 1.0, 1.0),
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO agent_operations "
                "(operation_id, kind, state, effect_disposition, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("bad-effect", "tool", "pending", "invalid", 1.0, 1.0),
            )
        )
