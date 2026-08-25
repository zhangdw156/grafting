"""Resolve environment directories by name under a configurable root."""

from __future__ import annotations

from pathlib import Path


def resolve_environment_dir(
    env_root: str | Path,
    env_name: str,
    *,
    project_root: Path | None = None,
) -> Path | None:
    """Resolve an environment directory by name under env_root.

    env_root can be absolute or relative. If relative, it is resolved
    against project_root (defaults to cwd).
    """
    root = Path(env_root)
    if not root.is_absolute():
        root = (project_root or Path.cwd()) / root
    root = root.resolve()

    normalized_name = str(env_name).strip()
    if not normalized_name:
        return None
    candidate = root / normalized_name
    return candidate.resolve() if candidate.exists() else None


def list_environments(
    env_root: str | Path,
    *,
    project_root: Path | None = None,
) -> list[str]:
    """List all environment names under env_root.

    env_root can be absolute or relative. If relative, it is resolved
    against project_root (defaults to cwd).
    """
    root = Path(env_root)
    if not root.is_absolute():
        root = (project_root or Path.cwd()) / root
    root = root.resolve()

    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / "runtime" / "backend.py").exists()
    )
