"""Dynamic environment loading: import backend, copy world.db, extract tools."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from grafting.environment.base import EnvironmentBackendProtocol
from grafting.environment.contract import resolve_env_contract


@dataclass(slots=True)
class LoadedEnvironmentBackend:
    env_dir: Path
    backend: EnvironmentBackendProtocol
    session_db_path: Path
    tool_names: tuple[str, ...]


def load_environment_backend(
    env_dir: Path,
    *,
    session_db_dir: Path | None = None,
) -> LoadedEnvironmentBackend:
    """Load an environment backend with a session-local DB copy."""
    contract = resolve_env_contract(env_dir)

    if not contract.world_db_path.exists():
        raise FileNotFoundError(f"world.db not found: {contract.world_db_path}")
    if not contract.backend_path.exists():
        raise FileNotFoundError(f"backend.py not found: {contract.backend_path}")

    if session_db_dir is None:
        session_db_dir = Path(tempfile.mkdtemp(prefix="grafting_session_"))
    session_db_path = session_db_dir / "session.db"
    shutil.copy2(contract.world_db_path, session_db_path)

    module = load_module_from_path(
        module_name=f"grafting_env_backend_{contract.env_dir.name}",
        file_path=contract.backend_path,
    )
    backend_cls = getattr(module, "EnvironmentBackend")
    backend = backend_cls(
        db_path=session_db_path,
    )

    tool_names = _load_tool_names_from_jsonl(contract.tools_jsonl_path)

    return LoadedEnvironmentBackend(
        env_dir=contract.env_dir,
        backend=backend,
        session_db_path=session_db_path,
        tool_names=tool_names,
    )


def load_module_from_path(*, module_name: str, file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from path: {file_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool_names_from_jsonl(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        name = parsed.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)
