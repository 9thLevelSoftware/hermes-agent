"""Tests for the deterministic fault fixtures themselves.

These verify the fakes behave predictably: the clock only moves when told,
faults fire exactly once as scheduled, call counts are exact, and every
fake resets to a pristine state. Scenario tests that USE these fakes live
elsewhere; here we only prove the fixtures are trustworthy.
"""

from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from tests.reliability.fakes import (
    FakeClock,
    FakeDBClosedError,
    FakeDBHandle,
    FakeDelivery,
    FakeFuture,
    FakeProvider,
    RateLimitResponse,
    ScenarioResult,
)


# ── FakeClock ────────────────────────────────────────────────────────────────

def test_clock_does_not_advance_on_its_own():
    clock = FakeClock(start=1000.0)
    assert clock.time() == 1000.0
    assert clock.time() == 1000.0  # repeated reads are stable
    assert clock.monotonic() == clock.time()


def test_clock_advances_only_when_told():
    clock = FakeClock(start=1000.0)
    assert clock.advance(5.0) == 1005.0
    assert clock.time() == 1005.0
    assert clock.monotonic() == 1005.0


def test_clock_rejects_negative_advance():
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_clock_reset_is_repeatable():
    clock = FakeClock(start=42.0)
    clock.advance(100.0)
    clock.reset()
    assert clock.time() == 42.0
    clock.advance(100.0)
    clock.reset()
    assert clock.time() == 42.0


# ── FakeFuture (tool cancellation / late completion) ─────────────────────────

def test_future_completes_and_returns_result():
    fut = FakeFuture()
    assert not fut.done
    fut.complete("value")
    assert fut.done
    assert fut.result() == "value"
    assert not fut.late_completion


def test_future_result_before_completion_raises():
    fut = FakeFuture()
    with pytest.raises(LookupError):
        fut.result()


def test_future_cancel_blocks_before_completion():
    fut = FakeFuture()
    assert fut.cancel() is True
    assert fut.cancelled
    assert fut.done
    assert fut.cancel() is False  # already terminal


def test_future_late_completion_after_cancel_is_flagged():
    fut = FakeFuture()
    fut.cancel()
    fut.complete("stale-result")
    assert fut.late_completion is True  # a cancelled call still produced work
    assert fut.result() == "stale-result"


def test_future_cannot_cancel_after_completion():
    fut = FakeFuture()
    fut.complete(1)
    assert fut.cancel() is False


# ── FakeProvider (rate-limit responses) ──────────────────────────────────────

def test_provider_serves_scheduled_rate_limits_then_succeeds():
    provider = FakeProvider(rate_limit_before_success=2, retry_after=3.0)
    first = provider.send("payload")
    second = provider.send("payload")
    third = provider.send("payload")
    assert isinstance(first, RateLimitResponse)
    assert isinstance(second, RateLimitResponse)
    assert first.retry_after == 3.0
    assert first.status == 429
    assert third == {"ok": True, "payload": "payload"}


def test_provider_call_count_is_exact():
    provider = FakeProvider(rate_limit_before_success=1)
    provider.send()
    provider.send()
    assert provider.calls == 2


def test_provider_reset_replays_the_same_faults():
    provider = FakeProvider(rate_limit_before_success=1)
    assert isinstance(provider.send(), RateLimitResponse)
    assert provider.send() == {"ok": True, "payload": None}
    provider.reset()
    assert provider.calls == 0
    assert isinstance(provider.send(), RateLimitResponse)  # fault fires again


# ── FakeDelivery (acknowledgement loss) ──────────────────────────────────────

def test_delivery_loses_ack_on_scheduled_attempt():
    delivery = FakeDelivery(lose_acks_on=(1,))
    first = delivery.send("msg")
    second = delivery.send("msg")
    assert first is None  # ack lost on attempt 1
    assert second == {"acked": True, "attempt": 2}


def test_delivery_records_side_effect_even_when_ack_lost():
    delivery = FakeDelivery(lose_acks_on=(1,))
    delivery.send("msg")  # ack lost, but the message WAS delivered
    delivery.send("msg")  # retry, delivered again -> duplicate side effect
    assert delivery.attempts == 2
    assert delivery.deliveries == ["msg", "msg"]


def test_delivery_reset_is_repeatable():
    delivery = FakeDelivery(lose_acks_on=(1,))
    delivery.send("a")
    delivery.reset()
    assert delivery.attempts == 0
    assert delivery.deliveries == []
    assert delivery.send("b") is None  # scheduled loss fires again


# ── FakeDBHandle (handle closure) ────────────────────────────────────────────

def test_db_handle_closes_after_scheduled_op_count():
    handle = FakeDBHandle(close_after=2)
    assert handle.execute() == 1
    assert handle.execute() == 2  # this op trips the closure
    assert not handle.is_open
    with pytest.raises(FakeDBClosedError):
        handle.execute()


def test_db_handle_manual_close_raises_on_use():
    handle = FakeDBHandle()
    assert handle.is_open
    handle.close()
    assert not handle.is_open
    with pytest.raises(FakeDBClosedError):
        handle.execute()


def test_db_handle_reset_reopens_and_zeroes_ops():
    handle = FakeDBHandle(close_after=1)
    handle.execute()
    assert not handle.is_open
    handle.reset()
    assert handle.is_open
    assert handle.ops == 0
    assert handle.execute() == 1


# ── ScenarioResult ───────────────────────────────────────────────────────────

def test_scenario_result_is_frozen_dataclass_with_expected_fields():
    result = ScenarioResult(
        name="rate_limit_recovery",
        passed=True,
        final_state="delivered",
        expected_state="delivered",
        unresolved_count=0,
        wrong_side_effect_count=0,
        recovery_steps=("retry", "ack"),
    )
    assert is_dataclass(result)
    assert result.name == "rate_limit_recovery"
    assert result.passed is True
    assert result.final_state == result.expected_state
    assert result.unresolved_count == 0
    assert result.wrong_side_effect_count == 0
    assert result.recovery_steps == ("retry", "ack")


def test_scenario_result_recovery_steps_default_empty():
    result = ScenarioResult(
        name="noop",
        passed=False,
        final_state="stuck",
        expected_state="done",
        unresolved_count=1,
        wrong_side_effect_count=0,
    )
    assert result.recovery_steps == ()


def test_scenario_result_is_immutable():
    result = ScenarioResult(
        name="x",
        passed=True,
        final_state="a",
        expected_state="a",
        unresolved_count=0,
        wrong_side_effect_count=0,
    )
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]
