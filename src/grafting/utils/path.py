from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT: Path | None = None
_PACKAGE_ROOT: Path | None = None


def get_package_root() -> Path:
    global _PACKAGE_ROOT
    if _PACKAGE_ROOT is None:
        _PACKAGE_ROOT = Path(__file__).resolve().parent.parent
    return _PACKAGE_ROOT


def get_project_root() -> Path:
    global _PROJECT_ROOT
    if _PROJECT_ROOT is None:
        _PROJECT_ROOT = get_package_root().parent.parent
    return _PROJECT_ROOT


def resolve_package_path(path: str | Path) -> Path:
    """Resolve a package resource in source and installed-wheel layouts."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()

    if candidate.parts[:2] == ("src", "grafting"):
        candidate = Path(*candidate.parts[2:])
    return (get_package_root() / candidate).resolve()


def get_settings_path() -> Path:
    return Path.cwd() / "settings.yaml"
