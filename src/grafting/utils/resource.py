from __future__ import annotations

from pathlib import Path

from grafting.utils.path import get_package_root


def package_path(*parts: str) -> Path:
    return get_package_root().joinpath(*parts)


def config_path(name: str) -> Path:
    return package_path("configs", name)


def read_text(*parts: str) -> str:
    return package_path(*parts).read_text(encoding="utf-8")
