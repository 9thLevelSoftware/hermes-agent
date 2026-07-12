"""Durable operation journal backed by the profile-local state database."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from hermes_state import SessionDB


_STATES = frozenset(
    {"pending", "running", "dispatched", "confirmed", "failed", "unknown", "cancelled"}
)
_EFFECTS = frozenset({"none", "landed", "unknown"})
_TERMINAL_STATES = frozenset({"confirmed", "failed", "unknown", "cancelled"})
_TRANSITIONS = {
    "pending": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"dispatched", "confirmed", "failed", "unknown", "cancelled"}),
    "dispatched": frozenset({"confirmed", "failed", "unknown"}),
    "confirmed": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
    "cancelled": frozenset(),
}


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    session_id: str
    turn_id: str
    tool_call_id: str
    destination: str
    payload_hash: str
    state: str
    effect_disposition: str
    result_json: Optional[str]
    error: Optional[str]
    created_at: float
    updated_at: float
    acknowledged_at: Optional[float]


class OperationJournal:
    """Record operation certainty without making uncertain work retryable."""

    def __init__(self, db: SessionDB):
        self._db = db

    @staticmethod
    def _record(row) -> OperationRecord:
        return OperationRecord(**dict(row))

    @staticmethod
    def _result_json(result: Any) -> Optional[str]:
        return None if result is None else json.dumps(result, sort_keys=True, default=str)

    @staticmethod
    def _validate_transition(
        from_states: set[str], to_state: str, effect_disposition: str
    ) -> None:
        if not from_states or not from_states <= _STATES:
            raise ValueError("invalid operation source state")
        if to_state not in _STATES:
            raise ValueError("invalid operation target state")
        if effect_disposition not in _EFFECTS:
            raise ValueError("invalid operation effect disposition")
        if any(to_state not in _TRANSITIONS[state] for state in from_states):
            raise ValueError("invalid operation state transition")

    def create(
        self,
        *,
        operation_id: str,
        kind: str,
        session_id: str = "",
        turn_id: str = "",
        tool_call_id: str = "",
        destination: str = "",
        payload_hash: str = "",
    ) -> OperationRecord:
        identity = (
            kind,
            session_id,
            turn_id,
            tool_call_id,
            destination,
            payload_hash,
        )

        def _create(conn):
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                existing_identity = tuple(row[field] for field in (
                    "kind",
                    "session_id",
                    "turn_id",
                    "tool_call_id",
                    "destination",
                    "payload_hash",
                ))
                if existing_identity != identity:
                    raise ValueError(
                        f"operation id {operation_id!r} already identifies a different operation"
                    )
                return row

            now = time.time()
            conn.execute(
                """INSERT INTO agent_operations (
                       operation_id, kind, session_id, turn_id, tool_call_id,
                       destination, payload_hash, state, effect_disposition,
                       result_json, error, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'none', NULL, NULL, ?, ?)""",
                (
                    operation_id,
                    kind,
                    session_id,
                    turn_id,
                    tool_call_id,
                    destination,
                    payload_hash,
                    now,
                    now,
                ),
            )
            return conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        return self._record(self._db._execute_write(_create))

    def transition(
        self,
        operation_id: str,
        *,
        from_states: set[str],
        to_state: str,
        effect_disposition: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> OperationRecord:
        self._validate_transition(from_states, to_state, effect_disposition)
        placeholders = ",".join("?" for _ in from_states)
        result_json = self._result_json(result)

        def _transition(conn):
            current = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if current is None:
                raise KeyError(operation_id)
            if current["state"] not in from_states:
                raise ValueError("stale operation state")

            now = time.time()
            cursor = conn.execute(
                f"""UPDATE agent_operations
                       SET state = ?, effect_disposition = ?, result_json = ?,
                           error = ?, updated_at = ?
                     WHERE operation_id = ? AND state IN ({placeholders})""",
                (
                    to_state,
                    effect_disposition,
                    result_json,
                    error,
                    now,
                    operation_id,
                    *from_states,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale operation state")
            return conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        return self._record(self._db._execute_write(_transition))

    def acknowledge(self, operation_id: str) -> bool:
        placeholders = ",".join("?" for _ in _TERMINAL_STATES)

        def _acknowledge(conn):
            cursor = conn.execute(
                f"""UPDATE agent_operations
                       SET acknowledged_at = ?
                     WHERE operation_id = ?
                       AND state IN ({placeholders})
                       AND acknowledged_at IS NULL""",
                (time.time(), operation_id, *_TERMINAL_STATES),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_acknowledge)

    def get(self, operation_id: str) -> Optional[OperationRecord]:
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list_unacknowledged(self, kind: Optional[str] = None) -> list[OperationRecord]:
        query = "SELECT * FROM agent_operations WHERE acknowledged_at IS NULL"
        params: list[str] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at, operation_id"
        with self._db._lock:
            rows = self._db._conn.execute(query, params).fetchall()
        return [self._record(row) for row in rows]

    def reconcile_after_restart(self) -> int:
        def _reconcile(conn):
            cursor = conn.execute(
                """UPDATE agent_operations
                       SET state = 'unknown', effect_disposition = 'unknown', updated_at = ?
                     WHERE state IN ('running', 'dispatched')""",
                (time.time(),),
            )
            return cursor.rowcount

        return self._db._execute_write(_reconcile)
