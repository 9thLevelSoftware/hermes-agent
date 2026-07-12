from __future__ import annotations

from typing import Any


def _td(name: str, description: str = "tool") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_measure_tool_schema_cost_reports_aggregates(monkeypatch):
    from scripts.measure_tool_schema_cost import measure_tool_schema_cost
    from tools import tool_search

    raw_defs = [_td("core_tool"), _td("deferred_tool", "x" * 200)]
    monkeypatch.setattr(
        tool_search,
        "classify_tools",
        lambda defs: ([defs[0]], [defs[1]]),
    )

    result = measure_tool_schema_cost(
        raw_defs,
        config=tool_search.ToolSearchConfig.from_raw({"enabled": "on"}),
        context_length=200_000,
    )

    assert set(result) == {
        "raw_tools",
        "raw_tokens",
        "visible_tools",
        "visible_tokens",
        "deferred_tools",
        "saved_tokens",
    }
    assert result["raw_tools"] == len(raw_defs)
    assert result["raw_tokens"] > 0
    assert result["visible_tools"] == 4  # one upfront tool plus three bridges
    assert result["visible_tokens"] > 0
    assert result["deferred_tools"] == 1
    assert result["saved_tokens"] == max(
        0, result["raw_tokens"] - result["visible_tokens"]
    )


def test_measure_tool_schema_cost_keeps_all_tools_when_threshold_skips(monkeypatch):
    from scripts.measure_tool_schema_cost import measure_tool_schema_cost
    from tools import tool_search

    raw_defs = [_td("core_tool"), _td("deferred_tool", "x" * 200)]
    monkeypatch.setattr(
        tool_search,
        "classify_tools",
        lambda defs: ([defs[0]], [defs[1]]),
    )

    result = measure_tool_schema_cost(
        raw_defs,
        config=tool_search.ToolSearchConfig.from_raw(
            {"enabled": "auto", "absolute_threshold_tokens": 20_000}
        ),
        context_length=200_000,
    )

    assert result["visible_tools"] == result["raw_tools"]
    assert result["visible_tokens"] == result["raw_tokens"]
    # This is the eligible deferrable catalog count, even when auto assembly
    # is inactive.
    assert result["deferred_tools"] == 1
    assert result["saved_tokens"] == 0


def test_current_cost_discovers_mcp_before_get_tool_definitions(monkeypatch):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    calls = []
    monkeypatch.setattr(
        script,
        "discover_mcp_tools",
        lambda: calls.append("discover"),
    )
    monkeypatch.setattr(
        script,
        "get_tool_definitions",
        lambda **kwargs: calls.append("get_tool_definitions") or [],
    )
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(script, "_resolve_active_context_length", lambda: 0)
    monkeypatch.setattr(script, "measure_tool_schema_cost", lambda *args, **kwargs: {})

    script._current_cost()

    assert calls == ["discover", "get_tool_definitions"]


def test_current_cost_forwards_runtime_context_length(monkeypatch):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    received = {}
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: None)
    monkeypatch.setattr(script, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(
        script,
        "_resolve_active_context_length",
        lambda: 123_456,
    )

    def capture_measurement(tool_defs, **kwargs):
        received.update(kwargs)
        return {}

    monkeypatch.setattr(script, "measure_tool_schema_cost", capture_measurement)

    script._current_cost()

    assert received["context_length"] == 123_456
