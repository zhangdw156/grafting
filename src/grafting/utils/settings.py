"""Load model and harness settings from ``settings.yaml``."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

_VALID_PROVIDERS: Final[set[str]] = {
    "anthropic",
    "openai_chat_completions",
    "openai_responses",
}
_VALID_HARNESS_BACKENDS: Final[set[str]] = {"claude_code", "codex", "opencode"}
_VALID_AGENT_PROVIDERS: Final[set[str]] = {
    "anthropic",
    "openai_chat_completions",
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    model: str
    provider: str
    api_key: str = ""
    base_url: str = ""


@dataclass(frozen=True, slots=True)
class HarnessRunnerSettings:
    backend: str
    endpoint: Endpoint
    timeout: int = 900


@dataclass(frozen=True, slots=True)
class AgentSettings:
    endpoint: Endpoint


@dataclass(frozen=True, slots=True)
class SimulationSettings:
    user_agent: AgentSettings
    assistant_agent: AgentSettings


@dataclass(frozen=True, slots=True)
class Settings:
    environment: HarnessRunnerSettings | None = None
    task: HarnessRunnerSettings | None = None
    simulation: SimulationSettings | None = None
    evaluation: HarnessRunnerSettings | None = None
    evolution: HarnessRunnerSettings | None = None


def is_openai_provider(provider: str) -> bool:
    return provider.startswith("openai_")


def uses_responses_api(provider: str) -> bool:
    return provider.endswith("_responses")


def uses_chat_completions_api(provider: str) -> bool:
    return provider.endswith("_chat_completions")


_cached_settings: Settings | None = None
_cached_path: Path | None = None
_settings_lock = threading.Lock()


def get_settings_path() -> Path:
    override = os.environ.get("GRAFTING_SETTINGS_FILE")
    if override:
        return Path(override).resolve()
    return Path.cwd() / "settings.yaml"


def load_settings(path: Path | None = None) -> Settings:
    global _cached_settings, _cached_path
    resolved = (path or get_settings_path()).resolve()
    with _settings_lock:
        if _cached_settings is not None and _cached_path == resolved:
            return _cached_settings
        settings = _parse_settings_file(resolved)
        _cached_settings = settings
        _cached_path = resolved
        return settings


def clear_settings_cache() -> None:
    global _cached_settings, _cached_path
    with _settings_lock:
        _cached_settings = None
        _cached_path = None


def _parse_settings_file(path: Path) -> Settings:
    if not path.exists():
        raise FileNotFoundError(
            f"settings.yaml not found: {path}\n"
            "Copy settings.yaml.example to settings.yaml and fill in your credentials."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"settings.yaml must be a YAML mapping, got {type(raw).__name__}"
        )
    return _build_settings(raw)


def _build_settings(raw: dict[str, Any]) -> Settings:
    return Settings(
        environment=_parse_harness_runner("environment", raw.get("environment")),
        task=_parse_harness_runner("task", raw.get("task")),
        simulation=_parse_simulation(raw.get("simulation")),
        evaluation=_parse_harness_runner("evaluation", raw.get("evaluation")),
        evolution=_parse_harness_runner("evolution", raw.get("evolution")),
    )


def _parse_endpoint(context: str, raw: Any) -> Endpoint:
    if not isinstance(raw, dict):
        raise ValueError(f"settings.yaml: {context} must be a mapping")

    model = raw.get("model", "")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"settings.yaml: {context}.model is required")

    provider = raw.get("provider", "")
    if not isinstance(provider, str) or provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"settings.yaml: {context}.provider must be one of "
            f"{sorted(_VALID_PROVIDERS)}"
        )

    api_key = str(raw.get("api_key", ""))
    base_url = str(raw.get("base_url", ""))
    if not api_key or not base_url:
        raise ValueError(
            f"settings.yaml: {context} requires both api_key and base_url"
        )

    return Endpoint(
        model=model.strip(),
        provider=provider,
        api_key=api_key,
        base_url=base_url,
    )


def _parse_harness_runner(
    key: str,
    raw: Any,
) -> HarnessRunnerSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"settings.yaml: '{key}' must be a mapping")

    backend = raw.get("backend", "")
    if not isinstance(backend, str) or backend not in _VALID_HARNESS_BACKENDS:
        raise ValueError(
            f"settings.yaml: {key}.backend must be one of "
            f"{sorted(_VALID_HARNESS_BACKENDS)}"
        )

    endpoint = _parse_endpoint(f"{key}.endpoint", raw.get("endpoint"))
    timeout = int(raw.get("timeout", 900))

    if backend == "claude_code" and endpoint.provider != "anthropic":
        raise ValueError(
            f"settings.yaml: {key}.endpoint requires anthropic "
            "for the claude_code backend"
        )
    if backend in {"codex", "opencode"} and endpoint.provider == "anthropic":
        raise ValueError(
            f"settings.yaml: {key}.endpoint requires an OpenAI-compatible provider"
        )
    if backend == "codex" and uses_chat_completions_api(endpoint.provider):
        raise ValueError(
            f"settings.yaml: {key}.endpoint requires openai_responses "
            "for the codex backend"
        )

    return HarnessRunnerSettings(
        backend=backend,
        endpoint=endpoint,
        timeout=timeout,
    )


def _parse_simulation(raw: Any) -> SimulationSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("settings.yaml: 'simulation' must be a mapping")

    user_raw = raw.get("user_agent")
    assistant_raw = raw.get("assistant_agent")
    if not isinstance(user_raw, dict):
        raise ValueError("settings.yaml: simulation.user_agent is required")
    if not isinstance(assistant_raw, dict):
        raise ValueError("settings.yaml: simulation.assistant_agent is required")

    return SimulationSettings(
        user_agent=_parse_agent_settings("simulation.user_agent", user_raw),
        assistant_agent=_parse_agent_settings(
            "simulation.assistant_agent",
            assistant_raw,
        ),
    )


def _parse_agent_settings(context: str, raw: dict[str, Any]) -> AgentSettings:
    endpoint = _parse_endpoint(f"{context}.endpoint", raw.get("endpoint"))
    if endpoint.provider not in _VALID_AGENT_PROVIDERS:
        raise ValueError(
            f"settings.yaml: {context} only supports "
            f"{sorted(_VALID_AGENT_PROVIDERS)}"
        )
    return AgentSettings(endpoint=endpoint)
