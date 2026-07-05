import pytest
from pydantic import ValidationError

from hermes_cli.workflows_spec import WorkflowSpec, validate_graph


def _minimal_spec():
    return {
        "id": "demo",
        "name": "Demo",
        "version": 1,
        "enabled": True,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {"type": "pass", "output": {"ok": True}},
            "done": {"type": "pass"},
        },
        "edges": [{"from": "start", "to": "done"}],
    }


def test_minimal_spec_validates():
    spec = WorkflowSpec.model_validate(_minimal_spec())
    validate_graph(spec)
    assert spec.id == "demo"


def test_unknown_edge_target_rejected():
    raw = _minimal_spec()
    raw["edges"] = [{"from": "start", "to": "missing"}]
    spec = WorkflowSpec.model_validate(raw)
    with pytest.raises(ValueError, match="unknown edge target"):
        validate_graph(spec)


def test_switch_requires_default_or_exhaustive_edges():
    raw = _minimal_spec()
    raw["nodes"]["route"] = {"type": "switch", "cases": []}
    raw["edges"] = [{"from": "start", "to": "route"}]
    spec = WorkflowSpec.model_validate(raw)
    with pytest.raises(ValueError, match="switch node route must define"):
        validate_graph(spec)


def test_bad_workflow_id_rejected():
    raw = _minimal_spec()
    raw["id"] = "Bad ID With Spaces"
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(raw)
