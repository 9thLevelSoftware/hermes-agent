"""Pydantic workflow definition models and cheap graph validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TriggerType = Literal["manual", "schedule", "webhook", "kanban_event"]
NodeType = Literal[
    "pass",
    "switch",
    "agent_task",
    "wait",
    "parallel",
    "join",
    "send_message",
    "fail",
    "subworkflow",
]


class TriggerSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: TriggerType
    id: str | None = None


class RetrySpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_attempts: int = Field(default=1, ge=1)
    delay_seconds: float = Field(default=0, ge=0)


class WorkspaceSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: NodeType
    output: Any = None
    cases: list[Any] = Field(default_factory=list)
    default: str | None = None
    profile: str | None = None
    prompt: str | None = None
    retry: RetrySpec | None = None
    workspace: WorkspaceSpec | None = None


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str
    version: int = Field(ge=1)
    enabled: bool = True
    triggers: list[TriggerSpec] = Field(default_factory=list)
    nodes: dict[str, NodeSpec] = Field(default_factory=dict)
    edges: list[EdgeSpec] = Field(default_factory=list)


def validate_graph(spec: WorkflowSpec) -> None:
    if not spec.nodes:
        raise ValueError("workflow must define at least one node")

    node_ids = set(spec.nodes)
    outgoing_sources: set[str] = set()

    for edge in spec.edges:
        source_base = edge.from_.split(".", 1)[0]
        if source_base not in node_ids:
            raise ValueError(f"unknown edge source: {edge.from_}")
        if edge.to not in node_ids:
            raise ValueError(f"unknown edge target: {edge.to}")
        outgoing_sources.add(source_base)

    for node_id, node in spec.nodes.items():
        if node.type == "switch" and node_id not in outgoing_sources and not node.default:
            raise ValueError(f"switch node {node_id} must define outgoing edges or default")
        if node.type == "agent_task" and (not node.profile or not node.prompt):
            raise ValueError(f"agent_task node {node_id} requires profile and prompt")
