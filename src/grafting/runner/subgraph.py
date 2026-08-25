"""Subgraph sampler for task generation.

Samples subgraphs from a tool_graph.json to control which mutation
tools each generated task must exercise.  The program layer owns
tool-coverage diversity; the harness agent creates tasks within the
given subgraph constraint.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grafting.environment import ToolEdge, ToolGraph, ToolNode, load_tool_graph


def _node_to_dict(node: ToolNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "category": node.category,
        "reads": list(node.reads),
        "writes": list(node.writes),
    }


def _edge_to_dict(edge: ToolEdge) -> dict[str, Any]:
    return {
        "from": edge.from_tool,
        "to": edge.to_tool,
        "type": edge.edge_type,
        "via": edge.via,
    }


@dataclass(frozen=True)
class SubgraphSample:
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    mutation_anchors: tuple[str, ...]
    fingerprint: frozenset[str]

    def to_prompt_text(self) -> str:
        mutation_lines: list[str] = []
        query_lines: list[str] = []
        for n in self.nodes:
            line = f"- {n['name']}"
            if n["category"] == "mutation":
                mutation_lines.append(line)
            else:
                query_lines.append(line)

        edge_lines: list[str] = []
        for e in self.edges:
            detail = e.get("via") or e.get("description", "")
            edge_lines.append(f"- {e['from']} → {e['to']} ({e.get('type', '')}, {detail})")

        parts = ["## Target Subgraph\n"]
        parts.append(
            "Your task MUST exercise the following tools. "
            "Design the demand so that fulfilling it requires calling "
            "these tools.\n"
        )

        if mutation_lines:
            parts.append("**Mutation tools (must be called):**")
            parts.extend(mutation_lines)
            parts.append("")
        if query_lines:
            label = (
                "**Query tools (discovery path):**"
                if mutation_lines
                else "**Query tools (must be called):**"
            )
            parts.append(label)
            parts.extend(query_lines)
        if edge_lines:
            parts.append("")
            parts.append("**Edges (data flow):**")
            parts.extend(edge_lines)

        return "\n".join(parts)


@dataclass(frozen=True)
class SamplerGraph:
    """Task-generation adapter over the canonical environment ToolGraph."""

    graph: ToolGraph

    @classmethod
    def load(cls, path: Path) -> SamplerGraph:
        return cls(load_tool_graph(path))

    @property
    def nodes(self) -> tuple[dict[str, Any], ...]:
        return tuple(_node_to_dict(n) for n in self.graph.nodes)

    @property
    def edges(self) -> tuple[dict[str, Any], ...]:
        return tuple(_edge_to_dict(e) for e in self.graph.edges)

    def mutation_nodes(self) -> list[dict[str, Any]]:
        return [_node_to_dict(n) for n in self.graph.mutation_tools]

    def predecessors(self, tool_name: str) -> list[str]:
        return [e.from_tool for e in self.graph.predecessors(tool_name)]

    def node_by_name(self, name: str) -> dict[str, Any] | None:
        node = self.graph.get_node(name)
        return _node_to_dict(node) if node is not None else None

    def induced_edges(self, node_names: frozenset[str]) -> list[dict[str, Any]]:
        return [
            _edge_to_dict(e)
            for e in self.graph.edges
            if e.from_tool in node_names and e.to_tool in node_names
        ]


class SubgraphSampler:
    def __init__(
        self,
        graph: SamplerGraph,
        *,
        max_anchors: int = 2,
        predecessor_depth: int = 2,
    ) -> None:
        self._graph = graph
        self._max_anchors = max_anchors
        self._predecessor_depth = predecessor_depth
        self._used: set[tuple[frozenset[str], str]] = set()

        mutations = [n["name"] for n in graph.mutation_nodes()]
        if mutations:
            self._anchors = mutations
        else:
            self._anchors = [n["name"] for n in graph.nodes]

    def sample(self, rule_mode: str = "") -> SubgraphSample | None:
        candidates = self._generate_candidates()
        random.shuffle(candidates)

        for combo in candidates:
            node_names = self._expand(combo)
            fp = frozenset(node_names)
            key = (fp, rule_mode)
            if key not in self._used:
                return self._build_sample(fp, combo)

        return None

    def mark_used(self, sample: SubgraphSample, rule_mode: str = "") -> None:
        self._used.add((sample.fingerprint, rule_mode))

    def mark_used_fingerprint(
        self,
        fingerprint: frozenset[str],
        rule_mode: str = "",
    ) -> None:
        self._used.add((fingerprint, rule_mode))

    def reset(self) -> None:
        self._used.clear()

    def exhausted(self, rule_mode: str = "") -> bool:
        candidates = self._generate_candidates()
        for combo in candidates:
            node_names = self._expand(combo)
            fp = frozenset(node_names)
            key = (fp, rule_mode)
            if key not in self._used:
                return False
        return True

    def _generate_candidates(self) -> list[tuple[str, ...]]:
        candidates: list[tuple[str, ...]] = []
        for m in self._anchors:
            candidates.append((m,))
        if self._max_anchors >= 2:
            for i, m1 in enumerate(self._anchors):
                for m2 in self._anchors[i + 1 :]:
                    candidates.append((m1, m2))
        if self._max_anchors >= 3:
            for i, m1 in enumerate(self._anchors):
                for j, m2 in enumerate(self._anchors[i + 1 :], i + 1):
                    for m3 in self._anchors[j + 1 :]:
                        candidates.append((m1, m2, m3))
        return candidates

    def _expand(self, anchors: tuple[str, ...]) -> set[str]:
        collected: set[str] = set(anchors)
        frontier = list(anchors)

        for _ in range(self._predecessor_depth):
            next_frontier: list[str] = []
            for name in frontier:
                for pred in self._graph.predecessors(name):
                    if pred not in collected:
                        collected.add(pred)
                        next_frontier.append(pred)
            frontier = next_frontier
            if not frontier:
                break

        return collected

    def _build_sample(
        self,
        node_names: frozenset[str],
        anchors: tuple[str, ...],
    ) -> SubgraphSample:
        nodes = tuple(n for n in self._graph.nodes if n["name"] in node_names)
        edges = tuple(self._graph.induced_edges(node_names))
        return SubgraphSample(
            nodes=nodes,
            edges=edges,
            mutation_anchors=anchors,
            fingerprint=node_names,
        )
