"""Environment contract: required file layout and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ENV_CONTRACT_REQUIRED_FILES: list[str] = [
    "spec/tools.jsonl",
    "spec/tool_graph.json",
    "spec/ENVIRONMENT.md",
    "runtime/backend.py",
    "world/world.db",
    "world/schema.sql",
]


@dataclass(frozen=True, slots=True)
class EnvContractPaths:
    env_dir: Path
    tools_jsonl_path: Path
    tool_graph_path: Path
    environment_md_path: Path
    backend_path: Path
    world_db_path: Path
    schema_sql_path: Path

    @property
    def tests_dir(self) -> Path:
        return self.env_dir / "tests"


def resolve_env_contract(env_dir: Path) -> EnvContractPaths:
    resolved = env_dir.resolve()
    return EnvContractPaths(
        env_dir=resolved,
        tools_jsonl_path=(resolved / "spec" / "tools.jsonl").resolve(),
        tool_graph_path=(resolved / "spec" / "tool_graph.json").resolve(),
        environment_md_path=(resolved / "spec" / "ENVIRONMENT.md").resolve(),
        backend_path=(resolved / "runtime" / "backend.py").resolve(),
        world_db_path=(resolved / "world" / "world.db").resolve(),
        schema_sql_path=(resolved / "world" / "schema.sql").resolve(),
    )


def is_valid_env_dir(env_dir: Path) -> bool:
    resolved = env_dir.resolve()
    return all((resolved / f).exists() for f in ENV_CONTRACT_REQUIRED_FILES)


def find_missing_env_files(env_dir: Path) -> list[str]:
    resolved = env_dir.resolve()
    return [f for f in ENV_CONTRACT_REQUIRED_FILES if not (resolved / f).exists()]
