import asyncio
import logging

from gateway import run as gateway_run
from gateway.run import GatewayRunner
from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli import workflows_dispatcher


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._running = True
    return runner


def test_workflow_dispatcher_disabled_does_not_tick(monkeypatch):
    runner = _runner()
    calls = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"workflow": {"dispatch_in_gateway": False}},
    )
    monkeypatch.setattr(workflows_dispatcher, "tick", lambda *, limit: calls.append(limit))

    asyncio.run(runner._workflow_dispatcher_watcher(initial_delay=0))

    assert calls == []


def test_workflow_dispatcher_enabled_ticks_on_cadence(monkeypatch):
    runner = _runner()
    calls = []
    sleeps = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "workflow": {
                "dispatch_in_gateway": True,
                "tick_interval_seconds": 2,
                "max_executions_per_tick": 7,
            }
        },
    )
    monkeypatch.setattr(workflows_dispatcher, "tick", lambda *, limit: calls.append(limit) or 1)

    async def fake_sleep(delay):
        sleeps.append(delay)
        runner._running = False

    monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

    asyncio.run(runner._workflow_dispatcher_watcher(initial_delay=0))

    assert calls == [7]
    assert sleeps == [2.0]


def test_workflow_dispatcher_failure_is_logged_and_loop_survives(monkeypatch, caplog):
    runner = _runner()
    sleeps = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "workflow": {
                "dispatch_in_gateway": True,
                "tick_interval_seconds": 1,
                "max_executions_per_tick": 3,
            }
        },
    )

    def fail_tick(*, limit):
        raise RuntimeError(f"boom limit={limit}")

    monkeypatch.setattr(workflows_dispatcher, "tick", fail_tick)

    async def fake_sleep(delay):
        sleeps.append(delay)
        runner._running = False

    monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(runner._workflow_dispatcher_watcher(initial_delay=0))

    assert sleeps == [1.0]
    assert "workflow dispatcher: tick failed" in caplog.text
    assert "boom limit=3" in caplog.text


def test_workflow_config_defaults_are_disabled():
    assert DEFAULT_CONFIG["workflow"] == {
        "dispatch_in_gateway": False,
        "tick_interval_seconds": 30,
        "max_executions_per_tick": 50,
    }
