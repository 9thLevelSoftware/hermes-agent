#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50


# ---------------------------------------------------------------------------
# Durable delegation completion (Task 8)
# ---------------------------------------------------------------------------
# Each dispatch gets a row in ``agent_operations`` (kind=async_delegation) so a
# crashed process leaves the result recoverable: the next process reads
# terminal unacked rows and re-enqueues them, with consumer-side ack preventing
# double-injection across restarts. We open the journal lazily and fall back to
# in-memory only if SessionDB is unavailable (the implementation has to keep
# working on developer laptops with no profile state).

_KIND = "async_delegation"

# Map a worker-supplied status string → (operation state, effect_disposition).
# Unknown statuses become state=unknown/effect=unknown — a durable "I don't
# know what happened" instead of silently dropping the result.
_STATUS_MAP = {
    "completed": ("confirmed", "none"),
    "success":   ("confirmed", "none"),
    "error":     ("failed", "none"),
    "failed":    ("failed", "none"),
    "interrupted": ("cancelled", "none"),
    "cancelled": ("cancelled", "none"),
}

_journal: Optional["OperationJournal"] = None
_journal_lock = threading.Lock()
# Delegation ids already restored from the durable journal into this process's
# completion queue. Idempotent within process scope: a second restore call in
# the same process must not double-enqueue. Cleared on process restart.
_restored_ids: set = set()
_restored_lock = threading.Lock()


def _open_journal():
    """Return a process-wide ``OperationJournal`` or None if state.db is down.

    Fail-open by design: callers MUST tolerate None and keep operating
    in-memory only. The journal is opened against the default profile's
    state.db; if the underlying ``SessionDB`` open raises (locked file,
    corrupt schema, missing dependency), we log and return None.
    """
    global _journal
    if _journal is not None:
        return _journal
    with _journal_lock:
        if _journal is not None:
            return _journal
        try:
            from agent.operation_journal import OperationJournal
            from hermes_state import SessionDB

            _journal = OperationJournal(SessionDB())
        except Exception as exc:  # noqa: BLE001 — fail-open by spec
            logger.warning(
                "Async delegation durable journal unavailable; "
                "completions will not survive process restart: %s",
                exc,
            )
            _journal = None
        return _journal


def _set_journal_for_tests(journal) -> None:
    """Test-only: bind a specific journal to bypass lazy open."""
    global _journal
    with _journal_lock:
        _journal = journal


def _journal_for_tests():
    """Test-only: read the current journal handle (may be None)."""
    return _journal


def acknowledge_async_delegation(operation_id: str) -> bool:
    """Mark a terminal async-delegation operation as consumed.

    Consumer side: call AFTER the completion event has been injected into the
    originating session so a process restart won't re-enqueue the same result.
    Returns True iff this call did the ack (False for unknown / not terminal /
    already acked).
    """
    journal = _open_journal()
    if journal is None:
        return False
    try:
        return bool(journal.acknowledge(operation_id))
    except Exception as exc:  # noqa: BLE001 — ack must never crash the caller
        logger.debug("acknowledge_async_delegation(%s) failed: %s", operation_id, exc)
        return False


def restore_unacknowledged_delegations(
    queue, put_fn
) -> int:
    """Re-enqueue terminal unacknowledged delegations onto ``queue`` via ``put_fn``.

    Returns the count enqueued. Call ONCE at process startup (CLI / gateway /
    TUI) AFTER ``journal.reconcile_after_restart()`` so in-flight rows become
    ``unknown`` (which is terminal in this journal).

    Idempotent within the current process: ids already restored are skipped
    on a second call. Idempotent across processes via the ``acknowledged_at``
    row column, set by the consumer on successful injection.
    """
    journal = _open_journal()
    if journal is None:
        return 0
    try:
        records = journal.list_unacknowledged(kind=_KIND)
    except Exception as exc:  # noqa: BLE001
        logger.warning("restore_unacknowledged_delegations: list failed: %s", exc)
        return 0

    enqueued = 0
    with _restored_lock:
        for record in records:
            if record.operation_id in _restored_ids:
                continue
            evt = _record_to_event(record)
            if evt is None:
                # Defensive: terminal-but-malformed → ack so we don't loop forever.
                try:
                    journal.acknowledge(record.operation_id)
                except Exception:
                    pass
                continue
            try:
                put_fn(evt)
            except Exception as exc:  # noqa: BLE001 — never crash the boot path
                logger.warning(
                    "restore_unacknowledged_delegations: put failed for %s: %s",
                    record.operation_id, exc,
                )
                continue
            _restored_ids.add(record.operation_id)
            enqueued += 1
    return enqueued


def _record_to_event(record) -> Optional[Dict[str, Any]]:
    """Reconstruct the bounded completion event from a persisted result_json."""
    if not record.result_json:
        return None
    try:
        evt = json.loads(record.result_json)
    except Exception:
        return None
    if not isinstance(evt, dict):
        return None
    # Stamp the canonical fields the formatter and consumer rely on.
    evt.setdefault("type", "async_delegation")
    evt.setdefault("delegation_id", record.operation_id)
    return evt


def _terminal_state_for(status: str) -> tuple:
    return _STATUS_MAP.get(status, ("unknown", "unknown"))


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegations currently running."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") == "running")


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    # Persistent pending → running row. Fail-open: a missing journal keeps
    # the in-memory record alive and lets the worker run; we just lose the
    # crash-recovery safety net for this dispatch.
    journal = _open_journal()
    if journal is not None:
        try:
            journal.create(
                operation_id=delegation_id,
                kind=_KIND,
                session_id=session_key or "",
                destination=parent_session_id or "",
                payload_hash=f"{delegation_id}|{dispatched_at:.6f}",
            )
            journal.transition(
                delegation_id,
                from_states={"pending"},
                to_state="running",
                effect_disposition="none",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Async delegation %s: durable create/running failed: %s",
                delegation_id, exc,
            )

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue.

    Order matters: persist terminal operation state to the durable journal
    BEFORE enqueueing, so a crash between enqueue and consumer-side ack does
    not lose the result. The consumer's acknowledge_async_delegation() then
    keeps a restart from re-enqueueing the same event.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        # Snapshot fields needed for the event while holding the lock.
        event_record = dict(record)
        _prune_completed_locked()

    evt = _build_completion_event(event_record, result, status)
    if evt is None:
        return
    _persist_completion(delegation_id, evt, status)
    _push_completion_event(evt)


def _build_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> Optional[Dict[str, Any]]:
    """Build the bounded async_delegation event. Pure / no I/O."""
    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()
    return {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }


def _persist_completion(
    delegation_id: str, evt: Dict[str, Any], status: str
) -> None:
    """Write the terminal operation state to the journal BEFORE queue publish.

    The event dict itself is stored as ``result_json`` so a process restart
    can faithfully reconstruct the queue payload via
    ``restore_unacknowledged_delegations``. Fail-open: a missing/broken
    journal must never crash the worker — the in-memory record is still
    pushed to the queue, we just lose crash-recovery for this dispatch.
    """
    journal = _open_journal()
    if journal is not None:
        to_state, effect = _terminal_state_for(status)
        try:
            journal.transition(
                delegation_id,
                from_states={"running"},
                to_state=to_state,
                effect_disposition=effect,
                result=evt,
                error=str(evt.get("error")) if evt.get("error") else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Async delegation %s: durable transition to %s failed: %s",
                delegation_id, to_state, exc,
            )


def _push_completion_event(evt: Dict[str, Any]) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly. Terminal state has already been
    persisted to the journal by ``_persist_completion``; a future restart can
    recover.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            evt.get("delegation_id"), exc,
        )
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result is durable in state.db so a restart will recover it: %s",
            evt.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    journal = _open_journal()
    if journal is not None:
        try:
            journal.create(
                operation_id=delegation_id,
                kind=_KIND,
                session_id=session_key or "",
                destination=parent_session_id or "",
                payload_hash=f"{delegation_id}|{dispatched_at:.6f}",
            )
            journal.transition(
                delegation_id,
                from_states={"pending"},
                to_state="running",
                effect_disposition="none",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Async delegation batch %s: durable create/running failed: %s",
                delegation_id, exc,
            )

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event.

    Same ordering contract as ``_finalize``: persist the terminal operation
    state to the durable journal BEFORE publishing to the completion queue.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        event_record = dict(record)
        _prune_completed_locked()

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    _persist_completion(delegation_id, evt, status)
    _push_completion_event(evt)


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k != "interrupt_fn"}
            for r in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers, _journal
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
    with _journal_lock:
        _journal = None
    with _restored_lock:
        _restored_ids.clear()
