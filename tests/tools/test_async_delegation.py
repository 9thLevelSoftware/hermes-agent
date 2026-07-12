"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry, format_process_notification


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "User is a philosopher",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=5)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_crashed_runner_produces_error_completion():
    def boom():
        raise RuntimeError("subagent exploded")

    r = ad.dispatch_async_delegation(
        goal="risky", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=boom, max_async_children=3,
    )
    assert r["status"] == "dispatched"
    evt = _drain_one()
    assert evt is not None
    assert evt["status"] == "error"
    text = format_process_notification(evt)
    assert text is not None
    assert "did not complete successfully" in text
    assert "subagent exploded" in text


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}

    def blocker():
        ev.wait(timeout=5)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt
    evt = _drain_one()
    assert evt is not None
    assert evt["status"] == "interrupted"


def test_completed_records_pruned_to_cap():
    # Run more than the retention cap quickly; ensure list doesn't grow forever.
    for i in range(ad._MAX_RETAINED_COMPLETED + 10):
        ad.dispatch_async_delegation(
            goal=f"t{i}", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=lambda: {"status": "completed", "summary": "ok"},
            max_async_children=ad._MAX_RETAINED_COMPLETED + 20,
        )
    # let workers finish
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ad.active_count() > 0:
        time.sleep(0.05)
    assert len(ad.list_async_delegations()) <= ad._MAX_RETAINED_COMPLETED


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=5)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_delegate_task_background_batch_runs_as_one_unit(monkeypatch):
    """A multi-item batch with background=True dispatches the WHOLE fan-out as
    ONE background unit (one handle, one async slot). The children run in
    parallel and join; the consolidated results come back as a single
    completion event when ALL of them finish."""
    import json
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    gate = threading.Event()

    def _blocking_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=5)
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }

    # Use monkeypatch (not a `with` block) so the patches stay active while the
    # background worker thread runs _execute_and_aggregate AFTER delegate_task
    # has already returned.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", _blocking_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        tasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
        background=True,
        parent_agent=parent,
    )

    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["count"] == 3
    assert parsed["delegation_id"].startswith("deleg_")
    assert parsed["goals"] == ["a", "b", "c"]
    # ONE background unit for the whole fan-out (not three), and the call
    # returned while all children are still blocked → chat not blocked.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1

    # Release the children; the whole batch joins and emits ONE event.
    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 3
    summaries = sorted(r["summary"] for r in evt["results"])
    assert summaries == ["done: a", "done: b", "done: c"]
    # The consolidated notification names all three tasks in one block.
    text = format_process_notification(evt)
    assert text is not None
    assert "TASK 1/3" in text and "TASK 2/3" in text and "TASK 3/3" in text
    assert "done: a" in text and "done: b" in text and "done: c" in text
    # No more events — it's a single combined completion, not N of them.
    assert _drain_one() is None


def test_model_dispatch_forces_background():
    """The MODEL-facing dispatch path forces background=True for any top-level
    delegation (single task OR batch), and keeps it off for an orchestrator
    subagent (depth > 0). Direct delegate_task() callers are unaffected (they
    keep the synchronous default)."""
    import tools.delegate_tool as dt
    from unittest.mock import MagicMock

    top = MagicMock()
    top._delegate_depth = 0
    sub = MagicMock()
    sub._delegate_depth = 1

    # Registry-fallback helper: top-level always background, regardless of
    # single vs batch; subagent never.
    assert dt._model_background_value({"goal": "x"}, top) is True
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, top
    ) is True
    assert dt._model_background_value({"tasks": [{"goal": "a"}]}, top) is True
    assert dt._model_background_value({"goal": "x"}, sub) is False
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, sub
    ) is False


def test_run_agent_dispatch_forces_background():
    """run_agent._dispatch_delegate_task — the live model path — forces
    background on for any top-level delegation (single OR batch) and off for a
    subagent."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        agent = _FakeAgent()
        run_agent.AIAgent._dispatch_delegate_task(agent, {"goal": "x"})
        assert captured["background"] is True

        run_agent.AIAgent._dispatch_delegate_task(
            agent, {"tasks": [{"goal": "a"}, {"goal": "b"}]}
        )
        assert captured["background"] is True

        sub = _FakeAgent()
        sub._delegate_depth = 1
        run_agent.AIAgent._dispatch_delegate_task(sub, {"goal": "x"})
        assert captured["background"] is False


def test_dispatch_never_forwards_model_toolsets():
    """The model has no toolsets argument — subagents always inherit the
    parent's toolsets. Even if a model smuggles a `toolsets` key into the
    tool-call args, the live dispatch path must NOT forward it to
    delegate_task (which no longer accepts it) and must not crash."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(
            _FakeAgent(), {"goal": "x", "toolsets": ["web", "terminal"]}
        )
    assert "toolsets" not in captured


def test_delegate_task_background_detaches_child_from_parent(monkeypatch):
    """A background child must NOT remain in parent._active_children —
    otherwise parent-turn interrupts / cache evicts / session close would
    kill the detached subagent mid-run."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=5)
        return {"task_index": 0, "status": "completed", "summary": "ok"}

    def build_and_register(**kw):
        # Mirror what the real _build_child_agent does: register the child
        # for interrupt propagation.
        parent._active_children.append(fake_child)
        return fake_child

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    with patch.object(dt, "_build_child_agent", side_effect=build_and_register), \
         patch.object(dt, "_run_single_child", side_effect=slow_child), \
         patch.object(dt, "_resolve_delegation_credentials", return_value=creds):
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)

    import json
    assert json.loads(out)["status"] == "dispatched"
    # Child detached immediately at dispatch, while it is still running.
    assert fake_child not in parent._active_children
    gate.set()
    assert _drain_one() is not None


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_enriches_routing_from_session_key():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt()
    runner._enrich_async_delegation_routing(evt)
    assert evt["platform"] == "telegram"
    assert evt["chat_id"] == "12345"
    assert evt["thread_id"] == "678"


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_watch_drain_requeues_async_without_looping():
    from gateway.run import _drain_gateway_watch_events

    q = queue.Queue()
    async_evt = _make_async_evt()
    watch_evt = {
        "type": "watch_match",
        "session_id": "proc_1",
        "command": "pytest",
        "pattern": "READY",
        "output": "READY",
    }
    q.put(async_evt)
    q.put(watch_evt)

    watch_events = _drain_gateway_watch_events(q)

    assert watch_events == [watch_evt]
    assert q.qsize() == 1
    assert q.get_nowait() == async_evt


def test_gateway_builds_routable_source_from_enriched_event():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt()
    runner._enrich_async_delegation_routing(evt)
    src = runner._build_process_event_source(evt)
    assert src is not None
    assert src.platform.value == "telegram"
    assert src.chat_id == "12345"


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


# ---------------------------------------------------------------------------
# Task 8 — durable delegation completion & acknowledgement
# ---------------------------------------------------------------------------
# Completion of a background delegation must persist to state.db via
# OperationJournal BEFORE the completion event is enqueued, so a crash
# between persist and enqueue cannot lose the result; consumers must
# acknowledge after successful injection so a redelivery on restart
# never double-fires.

import json as _json_t8
from pathlib import Path as _Path_t8

import pytest as _pytest_t8

from agent.operation_journal import OperationJournal as _OpJournal_t8
from hermes_state import SessionDB as _SessionDB_t8


@_pytest_t8.fixture()
def _journal_db(tmp_path):
    db = _SessionDB_t8(db_path=tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def _open_journal_db(path: _Path_t8) -> _SessionDB_t8:
    """Return a SECOND SessionDB instance against the same path."""
    return _SessionDB_t8(db_path=path)


def test_dispatch_creates_pending_record_and_completion_persists_before_queue(_journal_db):
    """At dispatch, OperationJournal sees a pending 'async_delegation' row.
    On completion, the row reaches a terminal state (confirmed/failed/...)
    AND the event is enqueued — never the other way around."""
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    # Replace _push_completion_event with a publisher that asserts the
    # terminal row is already persisted before the queue sees the event.
    ad._completion_publisher_spy = []
    original_push = ad._push_completion_event

    def _spy_push(evt):
        # By the time this fires, the journal must already hold the
        # terminal row for the same delegation_id.
        rec = journal.get(evt["delegation_id"])
        assert rec is not None, (
            f"queue published evt {evt['delegation_id']!r} BEFORE "
            "terminal transition was persisted"
        )
        assert rec.state in {"confirmed", "failed", "cancelled", "unknown"}, (
            f"unexpected terminal state {rec.state!r} for {evt['delegation_id']!r}"
        )
        # Mirror the normal put so consumer tests downstream still see it.
        from tools.process_registry import process_registry
        process_registry.completion_queue.put(dict(evt))
        ad._completion_publisher_spy.append((rec.state, rec.effect_disposition, evt["delegation_id"]))

    ad._push_completion_event = _spy_push
    try:
        def runner():
            return {"status": "completed", "summary": "ok",
                    "api_calls": 1, "duration_seconds": 0.1, "model": "m"}

        res = ad.dispatch_async_delegation(
            goal="durable", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=runner, max_async_children=3,
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ad._completion_publisher_spy:
            time.sleep(0.02)

        assert res["status"] == "dispatched"
        assert len(ad._completion_publisher_spy) == 1
        state, effect, did = ad._completion_publisher_spy[0]
        assert state == "confirmed"
        assert effect == "none"
        assert did == res["delegation_id"]
    finally:
        ad._push_completion_event = original_push


def test_completed_persists_to_db_with_bounded_event_dict(_journal_db):
    """On completion, result_json holds the bounded event dict with the
    fields the consumer needs (summary, status, error, dispatched_at, etc.)"""
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def runner():
        return {"status": "completed", "summary": "the answer",
                "error": None, "api_calls": 4, "duration_seconds": 1.2,
                "model": "m", "exit_reason": "completed"}

    res = ad.dispatch_async_delegation(
        goal="bg", context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:1",
        parent_session_id="parent-1",
        runner=runner, max_async_children=3,
    )
    _drain_one()

    record = journal.get(res["delegation_id"])
    assert record is not None
    assert record.state == "confirmed"
    assert record.effect_disposition == "none"
    assert record.kind == "async_delegation"
    payload = _json_t8.loads(record.result_json)
    assert payload["status"] == "completed"
    assert payload["summary"] == "the answer"
    assert payload["session_key"] == "agent:main:telegram:dm:1"
    assert payload["parent_session_id"] == "parent-1"
    assert payload["model"] == "m"


def test_failed_completion_persists_with_failed_state(_journal_db):
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def boom():
        raise RuntimeError("kaboom")

    res = ad.dispatch_async_delegation(
        goal="x", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=boom, max_async_children=3,
    )
    _drain_one()

    record = journal.get(res["delegation_id"])
    assert record is not None
    assert record.state == "failed"
    assert record.effect_disposition == "none"
    assert "kaboom" in (record.error or "")


def test_interrupted_completion_persists_with_cancelled_state(_journal_db):
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    ev = threading.Event()

    def blocker():
        ev.wait(timeout=5)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        ev.set()

    res = ad.dispatch_async_delegation(
        goal="block", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, interrupt_fn=interrupt_fn,
        max_async_children=3,
    )
    ad.interrupt_all(reason="t")
    _drain_one()

    record = journal.get(res["delegation_id"])
    assert record is not None
    assert record.state == "cancelled"
    assert record.effect_disposition == "none"


def test_unknown_status_persists_with_unknown_state(_journal_db):
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def runner():
        return {"status": "weird", "summary": "?"}

    res = ad.dispatch_async_delegation(
        goal="weird", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    _drain_one()

    record = journal.get(res["delegation_id"])
    assert record is not None
    assert record.state == "unknown"
    assert record.effect_disposition == "unknown"


def test_acknowledge_marks_record_and_is_idempotent(_journal_db):
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def runner():
        return {"status": "completed", "summary": "ok"}

    res = ad.dispatch_async_delegation(
        goal="ack", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    _drain_one()
    assert journal.get(res["delegation_id"]).acknowledged_at is None

    assert ad.acknowledge_async_delegation(res["delegation_id"]) is True
    assert ad.acknowledge_async_delegation(res["delegation_id"]) is False
    assert ad.acknowledge_async_delegation(res["delegation_id"]) is False
    assert journal.get(res["delegation_id"]).acknowledged_at is not None


def test_acknowledge_pending_or_unknown_id_returns_false(_journal_db):
    """If the row hasn't reached a terminal state yet (still pending/running),
    acknowledge is a no-op and returns False. Once terminal, ack is True."""
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    # Block the worker long enough for the test thread to observe pre-terminal state.
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {"status": "completed", "summary": "x"}

    res = ad.dispatch_async_delegation(
        goal="pending", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    # Worker is blocked on gate → row is still "running", not terminal.
    # acknowledge must return False (only terminal rows can be acked).
    assert ad.acknowledge_async_delegation(res["delegation_id"]) is False

    # Let the worker finish + terminal transition land.
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rec = journal.get(res["delegation_id"])
        if rec and rec.state in {"confirmed", "failed", "cancelled", "unknown"}:
            break
        time.sleep(0.02)

    # After terminal transition, can be acknowledged.
    assert ad.acknowledge_async_delegation(res["delegation_id"]) is True


def test_restore_unacknowledged_returns_count_and_enqueues(tmp_path):
    """A fresh process opening the same db must see terminal unacked
    delegations as 'to enqueue' — idem-potent within process scope."""
    db_path = tmp_path / "state.db"

    # Process 1: dispatch + finish a delegation
    db1 = _SessionDB_t8(db_path=db_path)
    journal1 = _OpJournal_t8(db1)
    ad._set_journal_for_tests(journal1)

    def runner():
        return {"status": "completed", "summary": "delivered"}

    res = ad.dispatch_async_delegation(
        goal="persist", context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:7",
        parent_session_id="p-7",
        runner=runner, max_async_children=3,
    )
    _drain_one()
    db1.close()

    # Process 2 — fresh SessionDB instance, fresh journal
    db2 = _SessionDB_t8(db_path=db_path)
    journal2 = _OpJournal_t8(db2)
    ad._set_journal_for_tests(journal2)

    captured = []

    def _capture_put(evt):
        captured.append(evt)

    # Restore must return the count and requeue events
    enqueued = ad.restore_unacknowledged_delegations(
        process_registry.completion_queue, _capture_put,
    )
    assert enqueued == 1
    assert len(captured) == 1
    evt = captured[0]
    assert evt["type"] == "async_delegation"
    assert evt["delegation_id"] == res["delegation_id"]
    assert evt["session_key"] == "agent:main:telegram:dm:7"
    assert evt["status"] == "completed"
    assert evt["summary"] == "delivered"

    # Idempotent within process: a second restore within the same process
    # in the same in-memory cache must NOT double-enqueue.
    enqueued2 = ad.restore_unacknowledged_delegations(
        process_registry.completion_queue, _capture_put,
    )
    assert enqueued2 == 0
    assert len(captured) == 1

    # ...but a fresh process (new restore cache) DOES re-emit on restart.
    # ``_reset_for_tests()`` simulates process death: it clears the per-process
    # restored-ids cache + closes the prior journal handle, the same way a real
    # process boundary would.
    db2.close()
    ad._reset_for_tests()
    db3 = _SessionDB_t8(db_path=db_path)
    journal3 = _OpJournal_t8(db3)
    ad._set_journal_for_tests(journal3)
    captured2 = []
    enqueued3 = ad.restore_unacknowledged_delegations(
        process_registry.completion_queue,
        lambda e: captured2.append(e),
    )
    assert enqueued3 == 1
    assert len(captured2) == 1
    db3.close()


def test_acknowledge_via_restore_loop_skips_already_acked(_journal_db):
    """Once ack'd, restore_unacknowledged_delegations() must not re-enqueue
    on a subsequent call (with a fresh in-process cache that still loads
    only terminal+unacknowledged rows)."""
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def runner():
        return {"status": "completed", "summary": "ok"}

    res = ad.dispatch_async_delegation(
        goal="ack-then-restore", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=3,
    )
    _drain_one()
    ad.acknowledge_async_delegation(res["delegation_id"])

    captured = []
    enqueued = ad.restore_unacknowledged_delegations(
        process_registry.completion_queue, lambda e: captured.append(e),
    )
    assert enqueued == 0
    assert captured == []


def test_durable_journal_fail_open_when_db_unavailable(tmp_path, monkeypatch):
    """If state.db can't be opened, dispatch must still work — fail-open
    by logging and proceeding without persistence."""
    from tools import async_delegation as ad_mod

    # Force the journal helper to raise.
    class _BrokenDB:
        @staticmethod
        def _execute_write(_fn):
            raise RuntimeError("db corrupt")

    monkeypatch.setattr(ad_mod, "_open_journal", lambda: (_OpJournal_t8(_BrokenDB())))

    def runner():
        return {"status": "completed", "summary": "yes"}

    res = ad_mod.dispatch_async_delegation(
        goal="no-db", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"
    evt = _drain_one()
    assert evt is not None
    assert evt["status"] == "completed"
    # And acknowledge is a no-op (returns False).
    assert ad_mod.acknowledge_async_delegation("nonexistent") is False


def test_batch_dispatch_also_persists_as_one_record(_journal_db):
    journal = _OpJournal_t8(_journal_db)
    ad._set_journal_for_tests(journal)

    def runner():
        return {"status": "completed", "summary": None,
                "results": [
                    {"status": "completed", "summary": "a"},
                    {"status": "completed", "summary": "b"},
                ],
                "total_duration_seconds": 1.0}

    res = ad.dispatch_async_delegation_batch(
        goals=["a", "b"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    _drain_one()

    record = journal.get(res["delegation_id"])
    assert record is not None
    assert record.state == "confirmed"
    assert record.kind == "async_delegation"


def test_startup_reconcile_recovers_in_flight_after_process_restart(tmp_path):
    """Simulate a hard crash: process1 creates pending+running rows, then
    dies. process2 startup must reconcile them to 'unknown' on creation."""
    db_path = tmp_path / "state.db"
    db1 = _SessionDB_t8(db_path=db_path)
    journal1 = _OpJournal_t8(db1)

    journal1.create(operation_id="inflight-1", kind="async_delegation")
    journal1.create(operation_id="inflight-2", kind="async_delegation")
    journal1.transition(
        "inflight-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    # 'inflight-2' stays in pending.
    db1.close()

    db2 = _SessionDB_t8(db_path=db_path)
    journal2 = _OpJournal_t8(db2)
    moved = journal2.reconcile_after_restart()
    assert moved == 1
    assert journal2.get("inflight-1").state == "unknown"
    assert journal2.get("inflight-2").state == "pending"  # pending is NOT in-flight
    db2.close()


def test_startup_reconcile_then_restore_emits_orphans_only(tmp_path):
    db_path = tmp_path / "state.db"
    db1 = _SessionDB_t8(db_path=db_path)
    journal1 = _OpJournal_t8(db1)

    # An 'orphaned' terminal row: confirmed, never acknowledged.
    journal1.create(operation_id="orphan-1", kind="async_delegation")
    journal1.transition(
        "orphan-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal1.transition(
        "orphan-1",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="none",
        result={"status": "completed", "summary": "save me"},
    )

    # An already-acked row: must not re-emit.
    journal1.create(operation_id="acked-1", kind="async_delegation")
    journal1.transition(
        "acked-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal1.transition(
        "acked-1",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="none",
        result={"status": "completed", "summary": "delivered"},
    )
    journal1.acknowledge("acked-1")

    db1.close()

    db2 = _SessionDB_t8(db_path=db_path)
    journal2 = _OpJournal_t8(db2)
    journal2.reconcile_after_restart()

    ad._set_journal_for_tests(journal2)
    captured = []
    n = ad.restore_unacknowledged_delegations(
        process_registry.completion_queue, lambda e: captured.append(e),
    )
    assert n == 1
    assert len(captured) == 1
    assert captured[0]["delegation_id"] == "orphan-1"
    assert captured[0]["summary"] == "save me"
    db2.close()


