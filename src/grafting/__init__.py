"""
Grafting — Agentic Skill & TRAce

面向高质量多轮工具调用对话轨迹的数据工厂，用于微调小模型以提升 Agent 能力。
"""

from pathlib import Path

try:
    from importlib.metadata import version as _meta_version

    __version__ = _meta_version("grafting")
except Exception:
    package_version = Path(__file__).parent / "VERSION"
    source_version = Path(__file__).parent.parent / "VERSION"
    version_file = package_version if package_version.exists() else source_version
    __version__ = version_file.read_text().strip()
