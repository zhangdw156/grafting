"""Tool graph data model.

Defines the typed structure for tool_graph.json: nodes (tools with
read/write table declarations) and edges (data dependencies and state
preconditions between tools).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolNode:
    name: str
    category: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolEdge:
    from_tool: str
    to_tool: str
    edge_type: str
    via: str


@dataclass(frozen=True, slots=True)
class ToolGraph:
    nodes: tuple[ToolNode, ...] = ()
    edges: tuple[ToolEdge, ...] = ()

    _node_index: dict[str, ToolNode] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        index = {n.name: n for n in self.nodes}
        object.__setattr__(self, "_node_index", index)

    @classmethod
    def load(cls, path: Path) -> ToolGraph:
        return load_tool_graph(path)

    def get_node(self, name: str) -> ToolNode | None:
        return self._node_index.get(name)

    def successors(self, name: str) -> list[ToolEdge]:
        return [e for e in self.edges if e.from_tool == name]

    def predecessors(self, name: str) -> list[ToolEdge]:
        return [e for e in self.edges if e.to_tool == name]

    @property
    def tool_names(self) -> list[str]:
        return [n.name for n in self.nodes]

    @property
    def mutation_tools(self) -> list[ToolNode]:
        return [n for n in self.nodes if n.category == "mutation"]

    @property
    def query_tools(self) -> list[ToolNode]:
        return [n for n in self.nodes if n.category == "query"]


def _node_name(node: dict[str, Any]) -> str:
    if "name" in node:
        value = node["name"]
    elif "tool" in node:
        value = node["tool"]
    else:
        raise KeyError("name")

    if not isinstance(value, str) or not value:
        raise ValueError("tool graph nodes must define a non-empty string name")
    return value


def load_tool_graph(path: Path) -> ToolGraph:
    """Load a ToolGraph from a tool_graph.json file."""
    data = json.loads(path.read_text(encoding="utf-8"))

    nodes = tuple(
        ToolNode(
            name=_node_name(n),
            category=n["category"],
            reads=tuple(n.get("reads", ())),
            writes=tuple(n.get("writes", ())),
        )
        for n in data.get("nodes", ())
    )

    edges = tuple(
        ToolEdge(
            from_tool=e["from"],
            to_tool=e["to"],
            edge_type=e.get("type", e.get("kind", "")),
            via=e.get("via") or e.get("description") or e.get("reason") or "",
        )
        for e in data.get("edges", ())
    )

    return ToolGraph(nodes=nodes, edges=edges)
