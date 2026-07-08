import pytest

from hermes_cli.workflows_capabilities import (
    IMPLEMENTED_NODE_TYPES,
    IMPLEMENTED_TRIGGER_TYPES,
    UNSUPPORTED_NODE_TYPES,
    UNSUPPORTED_TRIGGER_TYPES,
    implemented_primitive_errors,
    require_implemented_primitives,
    workflow_capabilities,
)
from hermes_cli.workflows_spec import WorkflowSpec


def test_capabilities_separate_implemented_from_unsupported():
    assert IMPLEMENTED_TRIGGER_TYPES == {"manual", "schedule"}
    assert UNSUPPORTED_TRIGGER_TYPES == {"webhook", "kanban_event"}
    assert IMPLEMENTED_NODE_TYPES == {
        "pass",
        "switch",
        "agent_task",
        "wait",
        "parallel",
        "join",
        "fail",
    }
    assert UNSUPPORTED_NODE_TYPES == {"send_message", "subworkflow"}


def test_capabilities_payload_is_dashboard_friendly():
    payload = workflow_capabilities()
    assert payload["triggers"]["implemented"] == ["manual", "schedule"]
    assert payload["nodes"]["unsupported"] == ["send_message", "subworkflow"]
    assert payload["assistant"]["allowed_triggers"] == ["manual", "schedule"]
    assert "agent_task" in payload["assistant"]["allowed_nodes"]


def _spec_with_node(node_type: str) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "id": "unsupported_demo",
            "name": "Unsupported Demo",
            "version": 1,
            "triggers": [{"type": "manual"}],
            "nodes": {"start": {"type": node_type, "output": {}}},
            "edges": [],
        }
    )


def test_implemented_primitive_errors_reports_unsupported_node():
    spec = _spec_with_node("send_message")

    assert implemented_primitive_errors(spec) == [
        "unsupported node type: send_message on node start"
    ]


def test_require_implemented_primitives_raises_actionable_error():
    spec = WorkflowSpec.model_validate(
        {
            "id": "unsupported_trigger_demo",
            "name": "Unsupported Trigger Demo",
            "version": 1,
            "triggers": [{"type": "webhook"}],
            "nodes": {"start": {"type": "pass", "output": {}}},
            "edges": [],
        }
    )

    with pytest.raises(ValueError, match="unsupported trigger type: webhook"):
        require_implemented_primitives(spec)
