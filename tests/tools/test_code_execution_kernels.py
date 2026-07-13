import json
import os
import time
from unittest.mock import patch

import psutil
import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools import code_execution_tool
from tools.code_execution_tool import (
    ExecutionKernelRegistry,
    cleanup_execution_kernels,
    execute_code,
)


@pytest.fixture(autouse=True)
def _local_kernel_environment(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    yield
    cleanup_execution_kernels("kernel-test")
    cleanup_execution_kernels("kernel-a")
    cleanup_execution_kernels("kernel-b")
    cleanup_execution_kernels("kernel-timeout")


def _run(code, task_id, **kwargs):
    kernel_id = kwargs.pop("kernel_id", "test")
    return json.loads(
        execute_code(
            code,
            task_id=task_id,
            enabled_tools=[],
            persistent=True,
            kernel_id=kernel_id,
            **kwargs,
        )
    )


def test_persistent_kernel_preserves_state_and_fresh_calls_do_not():
    first = _run("counter = 41", "kernel-test")
    second = _run("print(counter + 1)", "kernel-test")

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["output"].strip() == "42"

    fresh_first = json.loads(
        execute_code("counter = 41", task_id="kernel-test", enabled_tools=[])
    )
    fresh_second = json.loads(
        execute_code("print(counter + 1)", task_id="kernel-test", enabled_tools=[])
    )
    assert fresh_first["status"] == "success"
    assert fresh_second["status"] == "error"
    assert "NameError" in fresh_second["output"]


def test_config_persistent_default_is_opt_in_without_overriding_explicit_false(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "code_execution:\n  persistent: true\n  kernel_idle_ttl: 900\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    try:
        first = json.loads(execute_code(
            "counter = 41", task_id="kernel-config", enabled_tools=[], kernel_id="configured",
        ))
        second = json.loads(execute_code(
            "print(counter + 1)", task_id="kernel-config", enabled_tools=[], kernel_id="configured",
        ))
        fresh_first = json.loads(execute_code(
            "counter = 41", task_id="kernel-config-fresh", enabled_tools=[], persistent=False,
        ))
        fresh_second = json.loads(execute_code(
            "print(counter + 1)", task_id="kernel-config-fresh", enabled_tools=[], persistent=False,
        ))
    finally:
        cleanup_execution_kernels("kernel-config")
        cleanup_execution_kernels("kernel-config-fresh")

    assert first["persistent"] is True
    assert second["output"].strip() == "42"
    assert fresh_first["status"] == "success"
    assert fresh_second["status"] == "error"


def test_persistent_kernel_reuses_existing_tool_rpc_channel():
    with patch(
        "model_tools.handle_function_call",
        return_value=json.dumps({"output": "rpc-ok", "exit_code": 0}),
    ):
        result = _run(
            "from hermes_tools import terminal\nprint(terminal('echo rpc'))",
            "kernel-test",
        )
    assert result["status"] == "success"
    assert "rpc-ok" in result["output"]
    assert result["tool_calls_made"] == 1


def test_reset_clears_state_and_timeout_restarts_clean_kernel():
    assert _run("secret = 'stale'", "kernel-test")["status"] == "success"
    reset = _run("", "kernel-test", reset=True)
    assert reset["status"] == "success"
    assert reset["kernel_reset"] is True
    assert "stale" not in _run("print(secret)", "kernel-test")["output"]

    assert _run("value = 7", "kernel-timeout")["status"] == "success"
    timed_out = _run("while True: pass", "kernel-timeout", timeout=0.15)
    assert timed_out["status"] == "timeout"
    assert timed_out["timed_out"] is True
    clean = _run("print('clean')", "kernel-timeout")
    assert clean["status"] == "success"
    assert clean["output"].strip() == "clean"
    assert "value" not in clean["output"]


def test_same_kernel_id_isolated_between_tasks():
    assert _run("value = 'task-a'", "kernel-a")["status"] == "success"
    assert _run("value = 'task-b'", "kernel-b")["status"] == "success"
    assert _run("print(value)", "kernel-a")["output"].strip() == "task-a"
    assert _run("print(value)", "kernel-b")["output"].strip() == "task-b"


def test_idle_expiry_terminates_and_removes_kernel():
    now = [100.0]
    registry = ExecutionKernelRegistry(clock=lambda: now[0], idle_ttl=1.0)
    old_registry = code_execution_tool._kernel_registry
    code_execution_tool._kernel_registry = registry
    try:
        result = _run("value = 1", "kernel-test")
        assert result["status"] == "success"
        kernel = registry.get("kernel-test", "test")
        assert kernel is not None
        pid = kernel.process.pid
        tmpdir = kernel._tmpdir

        now[0] += 2.0
        assert registry.cleanup_expired() == 1
        assert registry.get("kernel-test", "test") is None
        assert not os.path.exists(tmpdir)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and psutil.pid_exists(pid):
            time.sleep(0.02)
        assert not psutil.pid_exists(pid)
    finally:
        registry.close_all()
        code_execution_tool._kernel_registry = old_registry


def test_cleanup_is_task_scoped_idempotent_and_reaps_children():
    assert _run("value = 'a'", "kernel-a", kernel_id="one")["status"] == "success"
    assert _run("value = 'a'", "kernel-a", kernel_id="two")["status"] == "success"
    assert _run("value = 'b'", "kernel-b", kernel_id="one")["status"] == "success"

    registry = code_execution_tool._kernel_registry
    task_a_pids = [
        kernel.process.pid
        for kernel in registry.for_task("kernel-a").values()
    ]
    task_a_tmpdirs = [
        kernel._tmpdir
        for kernel in registry.for_task("kernel-a").values()
    ]
    task_b_pids = [
        kernel.process.pid
        for kernel in registry.for_task("kernel-b").values()
    ]
    assert len(task_a_pids) == 2
    assert len(task_b_pids) == 1

    cleanup_execution_kernels("kernel-a")
    cleanup_execution_kernels("kernel-a")
    assert registry.for_task("kernel-a") == {}
    assert registry.for_task("kernel-b")
    assert all(not os.path.exists(tmpdir) for tmpdir in task_a_tmpdirs)

    for pid in task_a_pids:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and psutil.pid_exists(pid):
            time.sleep(0.02)
        assert not psutil.pid_exists(pid)
    assert all(psutil.pid_exists(pid) for pid in task_b_pids)
