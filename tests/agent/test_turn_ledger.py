"""Tests for the per-turn outcome ledger.

Two surfaces covered here:

1. ``agent.turn_ledger`` — the dataclass + safe writer that the finalizers
   call. Owns no schema.
2. ``hermes_state.SessionDB`` — the four ledger methods
   (``record_turn_outcome``, ``annotate_turn_feedback``,
   ``get_outcome_trends``, ``get_skill_outcome_counts``) and the schema
   that backs them.

The finalizer / codex-runtime wiring is exercised in the matching focused
suites (``test_turn_finalizer_memory_gating``,
``test_codex_app_server_persist``); this file stays focused on the data
contract and the SQL round-trip.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_ledger import (
    TurnOutcomeRecord,
    record_turn_outcome_safely,
)
from agent.turn_outcome import TURN_OUTCOMES, classify_turn_outcome
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Dataclass shape — guards the frozen contract the finalizers depend on.
# ---------------------------------------------------------------------------


def test_turn_outcome_record_is_frozen_dataclass():
    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response(finish_reason=stop)",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan", "web"),
        model="test-model",
    )
    # frozen=True: any attribute mutation raises.
    with pytest.raises((AttributeError, Exception)):
        record.outcome = "failed"  # type: ignore[misc]


def test_skills_loaded_is_tuple_not_list_for_frozen_hashability():
    # skills_loaded needs to be a tuple so the dataclass stays hashable; a
    # plain list would silently pass the constructor and then break any
    # later set/dict usage.
    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response",
        api_calls=0,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan",),
        model="m",
    )
    assert isinstance(record.skills_loaded, tuple)
    # Hashable so callers can use it in sets / as dict keys if needed.
    hash(record)


# ---------------------------------------------------------------------------
# Safe writer — must never raise through finalization, even on DB errors.
# ---------------------------------------------------------------------------


class _BoomSessionDB:
    """Drop-in SessionDB stand-in whose record method raises."""

    def record_turn_outcome(self, _record):  # pragma: no cover - reached
        raise RuntimeError("simulated DB outage")


class _CaptureSessionDB:
    """Captures every record call so we can assert the wiring."""

    def __init__(self):
        self.calls = []

    def record_turn_outcome(self, record):
        self.calls.append(record)


def _record(**overrides) -> TurnOutcomeRecord:
    import time as _time
    base = dict(
        session_id="s1",
        turn_id="t1",
        created_at=_time.time(),
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=(),
        model="m",
    )
    base.update(overrides)
    return TurnOutcomeRecord(**base)


def test_record_turn_outcome_safely_swallows_db_errors():
    """A failing record path must NOT propagate through the finalizer."""
    db = _BoomSessionDB()
    record = _record()

    # Return None on failure, never raise.
    result = record_turn_outcome_safely(db, record)

    assert result is None


def test_record_turn_outcome_safely_passes_record_to_db():
    db = _CaptureSessionDB()
    record = _record(outcome="unresolved", outcome_reason="tool timeout")

    record_turn_outcome_safely(db, record)

    assert db.calls == [record]


# ---------------------------------------------------------------------------
# SessionDB: schema + round trip.
# ---------------------------------------------------------------------------


def _tmp_db():
    """Return a freshly-minted SessionDB rooted at a temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="turn_ledger_"))
    db = SessionDB(tmp / "state.db")
    return tmp, db


def test_turn_outcomes_table_exists_and_accepts_all_eight_outcomes(tmp_path=None):
    """The spec example: write a single TurnOutcomeRecord with all 8 columns
    populated, then read it back via get_outcome_trends + the JSON fields.
    Then cover the remaining 7 vocabulary values to prove the table accepts
    the full vocabulary (no CHECK constraint rejecting any canonical value).
    """
    tmp, db = _tmp_db()

    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=__import__("time").time(),
        outcome="unresolved",
        outcome_reason="tool timeout",
        turn_exit_reason="tool_timeout",
        api_calls=2,
        tool_iterations=1,
        retry_count=1,
        guardrail_halt=None,
        cost_usd_delta=0.12,
        input_tokens_delta=20,
        output_tokens_delta=4,
        cache_read_tokens_delta=10,
        skills_loaded=("plan", "web"),
        model="test-model",
    )
    db.record_turn_outcome(record)

    trends = db.get_outcome_trends(session_id="s1", days=30)
    assert trends, "get_outcome_trends must return at least one row"
    assert trends[0]["outcome"] == "unresolved"
    assert json.loads(trends[0]["skills_loaded"]) == ["plan", "web"]

    # Every canonical outcome value must persist without DB error.
    for outcome in TURN_OUTCOMES:
        db.record_turn_outcome(
            _record(
                session_id="s1",
                turn_id=f"t-{outcome}",
                outcome=outcome,
            )
        )
    all_rows = db.get_outcome_trends(session_id="s1", days=30)
    seen = {row["outcome"] for row in all_rows}
    assert seen == set(TURN_OUTCOMES)


def test_record_turn_outcome_round_trips_deltas_and_guardrail_json():
    tmp, db = _tmp_db()
    db.record_turn_outcome(
        _record(
            session_id="s1",
            turn_id="t-guardrail",
            outcome="blocked",
            outcome_reason="guardrail halted",
            guardrail_halt=json.dumps({"tool": "terminal", "reason": "deny"}),
            cost_usd_delta=0.5,
            input_tokens_delta=100,
            output_tokens_delta=10,
            cache_read_tokens_delta=0,
            skills_loaded=("plan",),
        )
    )
    rows = db.get_outcome_trends(session_id="s1", days=30)
    match = [r for r in rows if r["outcome"] == "blocked"][0]
    assert match["cost_usd_delta"] == pytest.approx(0.5)
    assert match["input_tokens_delta"] == 100
    assert match["output_tokens_delta"] == 10
    # guardrail_halt round-trips as JSON text (dict shape preserved).
    assert json.loads(match["guardrail_halt"]) == {
        "tool": "terminal",
        "reason": "deny",
    }


def test_record_turn_outcome_is_idempotent_per_turn():
    """``(session_id, turn_id)`` is unique — re-recording the same turn is
    a no-op (or upsert), never a duplicate."""
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="t1"))
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="t1", outcome="partial")
    )

    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert len(rows) == 1
    # The latest write wins.
    assert rows[0]["outcome"] == "partial"


# ---------------------------------------------------------------------------
# Feedback annotation.
# ---------------------------------------------------------------------------


def test_annotate_turn_feedback_writes_fields_and_dedupes_by_event_id():
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="t1"))

    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_up",
        value="helpful",
        source="user",
        event_id="evt-1",
    ) is True
    # Same event_id → no-op (idempotent).
    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_up",
        value="helpful",
        source="user",
        event_id="evt-1",
    ) is False
    # Different event_id → latest feedback wins for the scalar columns.
    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_down",
        value="too verbose",
        source="user",
        event_id="evt-2",
    ) is True

    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert rows
    # Scalar feedback columns reflect the most recent annotation.
    latest = rows[0]
    assert latest["feedback_kind"] == "thumbs_down"
    assert latest["feedback_value"] == "too verbose"
    assert latest["feedback_source"] == "user"
    assert latest["feedback_at"] is not None


def test_annotate_turn_feedback_returns_false_for_missing_turn():
    tmp, db = _tmp_db()
    # No row for s1/t-missing → annotation must report miss, not crash.
    assert (
        db.annotate_turn_feedback(
            "s1",
            "t-missing",
            kind="thumbs_up",
            value="",
            source="user",
            event_id="evt-x",
        )
        is False
    )


# ---------------------------------------------------------------------------
# Aggregations.
# ---------------------------------------------------------------------------


def test_get_outcome_trends_groups_by_outcome_and_session_filter():
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="a", outcome="verified"))
    db.record_turn_outcome(_record(session_id="s1", turn_id="b", outcome="verified"))
    db.record_turn_outcome(_record(session_id="s1", turn_id="c", outcome="failed"))
    db.record_turn_outcome(_record(session_id="s2", turn_id="a", outcome="verified"))

    s1 = db.get_outcome_trends(session_id="s1", days=30)
    by_outcome = {r["outcome"]: r["count"] for r in s1}
    assert by_outcome.get("verified") == 2
    assert by_outcome.get("failed") == 1
    # s2's verified row must NOT bleed into s1's projection.
    assert all(r["session_id"] == "s1" for r in s1)

    all_sessions = db.get_outcome_trends(days=30)
    by_outcome_all = {r["outcome"]: r["count"] for r in all_sessions}
    assert by_outcome_all.get("verified") == 3


def test_get_skill_outcome_counts_summarises_skills_loaded():
    tmp, db = _tmp_db()
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="a", outcome="verified",
                skills_loaded=("plan", "web"))
    )
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="b", outcome="verified",
                skills_loaded=("plan",))
    )
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="c", outcome="failed",
                skills_loaded=("plan",))
    )

    counts = {r["skill"]: r for r in db.get_skill_outcome_counts(days=30)}
    plan = counts["plan"]
    assert plan["verified"] == 2
    assert plan["failed"] == 1

    web = counts["web"]
    assert web["verified"] == 1
    assert web.get("failed", 0) == 0


# ---------------------------------------------------------------------------
# classify_turn_outcome still works the way the finalizer wires it up.
# ---------------------------------------------------------------------------


def test_classify_turn_outcome_vocabulary_matches_ledger_acceptance():
    """Every vocabulary value produced by classify must be a valid ledger
    outcome; cross-check via DB round-trip so the safe writer's contract
    matches classify's output set.
    """
    tmp, db = _tmp_db()
    for outcome in TURN_OUTCOMES:
        result = classify_turn_outcome(
            final_response="ok",
            failed=(outcome == "failed"),
            interrupted=(outcome == "interrupted"),
            _turn_exit_reason=(
                "unresolved" if outcome == "unresolved" else
                "approval_blocked" if outcome == "blocked" else
                "cancelled" if outcome == "cancelled" else
                "max_iterations_reached(60/60)" if outcome == "partial" else
                "text_response(finish_reason=stop)"
            ),
            verification_status=("passed" if outcome == "verified" else None),
            cancelled=(outcome == "cancelled"),
        )
        assert result["outcome"] == outcome
        db.record_turn_outcome(
            _record(
                session_id="s1",
                turn_id=f"t-{outcome}",
                outcome=result["outcome"],
                outcome_reason=result["reason"],
            )
        )
    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert {r["outcome"] for r in rows} == set(TURN_OUTCOMES)


# ---------------------------------------------------------------------------
# Finalizer integration — monkeypatch the safe writer to capture calls
# from both the chat-completions path (finalize_turn) and the codex app-server
# path (run_codex_app_server_turn). Asserts both finalizers produce
# canonical vocabulary and the same record schema.
# ---------------------------------------------------------------------------


class _RecordingSafeWriter:
    """Stand-in for record_turn_outcome_safely — captures every call."""

    def __init__(self):
        self.records = []

    def __call__(self, session_db, record):
        self.records.append(record)
        return None


class _RecordingSessionDB:
    def __init__(self):
        self.records = []

    def record_turn_outcome(self, record):
        self.records.append(record)


def _finalizer_agent(verification_status="passed"):
    from types import SimpleNamespace
    agent = SimpleNamespace(
        max_iterations=90,
        iteration_budget=SimpleNamespace(remaining=89, used=1, max_total=90),
        quiet_mode=True,
        model="test-model",
        provider="test-provider",
        base_url="",
        session_id="sess-finalizer-test",
        context_compressor=SimpleNamespace(last_prompt_tokens=0),
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="test",
        _tool_guardrail_halt_decision=None,
        _interrupt_message=None,
        _response_was_previewed=False,
        _skill_nudge_interval=0,
        _iters_since_skill=0,
        valid_tool_names=[],
        _turn_verification_status=verification_status,
        _memory_manager=MagicMock(),
        background_reviews=[],
        _session_db=_RecordingSessionDB(),
        _turn_token_cost_snapshot={},
        _current_turn_id="t-fin-1",
        # Hooks the finalizer calls — no-ops for the ledger test.
        _save_trajectory=lambda *a, **k: None,
        _cleanup_task_resources=lambda *a, **k: None,
        _drop_trailing_empty_response_scaffolding=lambda *a, **k: None,
        _persist_session=lambda *a, **k: None,
        _file_mutation_verifier_enabled=lambda: False,
        _turn_completion_explainer_enabled=lambda: False,
        _drain_pending_steer=lambda: None,
        clear_interrupt=lambda: None,
        # finalizer calls _sync_external_memory_for_turn unconditionally;
        # the real method routes through external-memory plugin hooks.
        # Stub it so this test exercises only the ledger hook.
        _sync_external_memory_for_turn=lambda **kwargs: None,
    )
    return agent


def test_finalizer_paths_call_safe_writer_with_canonical_vocabulary(monkeypatch):
    """Both finalizer paths (chat-completions and codex app-server) must
    funnel through record_turn_outcome_safely with one of the 8 canonical
    outcome values. Monkeypatch the writer to capture calls without
    touching state.db.
    """
    from agent import turn_finalizer, codex_runtime
    from agent.turn_outcome import TURN_OUTCOMES as _VOCAB
    from agent.turn_ledger import TurnOutcomeRecord

    recorder = _RecordingSafeWriter()
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely", recorder
    )
    # Both finalizers import the safe writer lazily inside their
    # try-block, so patching agent.turn_ledger.record_turn_outcome_safely
    # is enough — the lazy ``from agent.turn_ledger import ...`` resolves
    # at call time against the patched module.

    # ── Path 1: chat-completions finalizer ──────────────────────────
    agent = _finalizer_agent(verification_status="passed")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    turn_finalizer.finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "do it"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-1",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    # ── Path 2: codex app-server finalizer ───────────────────────────
    codex_agent = MagicMock()
    codex_agent.tool_progress_callback = None
    codex_agent._iters_since_skill = 0
    codex_agent._skill_nudge_interval = 0
    codex_agent.valid_tool_names = set()
    codex_agent._session_db = _RecordingSessionDB()
    codex_agent._session_db_created = True
    codex_agent.session_id = "sess-codex-test"
    codex_agent._current_turn_id = "t-codex-1"
    codex_agent._turn_token_cost_snapshot = {}
    codex_agent._sync_external_memory_for_turn = MagicMock()
    codex_agent._spawn_background_review = MagicMock()
    codex_agent._turn_verification_status = "passed"

    codex_turn = SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="t-codex-1",
        projected_messages=[{"role": "assistant", "content": "ok"}],
        tool_iterations=0,
        final_text="ok",
        should_retire=False,
    )
    codex_agent._codex_session = MagicMock()
    codex_agent._codex_session.run_turn.return_value = codex_turn

    codex_runtime.run_codex_app_server_turn(
        codex_agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task",
    )

    # ── Assertions ──────────────────────────────────────────────────
    assert len(recorder.records) == 2, (
        "Expected one record from each finalizer path, got "
        f"{len(recorder.records)}"
    )
    for record in recorder.records:
        assert isinstance(record, TurnOutcomeRecord)
        assert record.outcome in _VOCAB, (
            f"Non-canonical outcome {record.outcome!r}; expected one of {_VOCAB}"
        )
        # Schema sanity: every record carries the data-contract fields.
        for field in (
            "session_id", "turn_id", "created_at", "outcome",
            "api_calls", "tool_iterations", "skills_loaded", "model",
        ):
            assert hasattr(record, field), f"Record missing field: {field}"