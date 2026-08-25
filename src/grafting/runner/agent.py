from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from grafting.driver import HarnessBackend, build_harness_command
from grafting.runner.types import HarnessRunResult, MCPConfig
from grafting.utils import Endpoint, logger, uses_responses_api


@dataclass(slots=True)
class _XDGLayout:
    home: Path
    data: Path
    cache: Path
    config: Path
    state: Path
    tmp: Path

    @staticmethod
    def create(root: Path) -> _XDGLayout:
        resolved = root.resolve()
        layout = _XDGLayout(
            home=resolved / "home",
            data=resolved / "share",
            cache=resolved / "cache",
            config=resolved / "config",
            state=resolved / "state",
            tmp=resolved / "tmp",
        )
        for p in (layout.home, layout.data, layout.cache,
                  layout.config, layout.state, layout.tmp):
            p.mkdir(parents=True, exist_ok=True)
        return layout

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state),
            "TMPDIR": str(self.tmp),
        }


class HarnessAgent:

    def __init__(
        self,
        *,
        backend: HarnessBackend,
        endpoint: Endpoint,
        project_root: Path,
        xdg_path: Path,
        timeout: int = 600,
    ) -> None:
        self.backend = backend
        self.model = endpoint.model
        self.provider = endpoint.provider
        self.project_root = project_root
        self.timeout = timeout

        self._xdg = _XDGLayout.create(xdg_path)
        self._xdg_path = xdg_path
        self._env: dict[str, str] = {}

        self._env.update(self._xdg.env())
        self._setup_backend(endpoint)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        mcp: list[MCPConfig] | None = None,
    ) -> HarnessRunResult:
        env = os.environ.copy()
        env.update(self._env)

        model_for_cmd = self.model
        if self.backend == HarnessBackend.OPENCODE and self.model and "/" not in self.model:
            model_for_cmd = f"grafting-managed/{self.model}"

        cmd = build_harness_command(self.backend, prompt, model=model_for_cmd)

        logger.debug(
            "HarnessAgent.run backend={} model={} cwd={}",
            self.backend.value, self.model, self.project_root,
        )
        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                env=env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            logger.warning(
                "HarnessAgent timed out after {:.1f}s, backend={}",
                duration, self.backend.value,
            )
            return HarnessRunResult(
                exit_code=-1,
                stdout="",
                stderr=f"timeout after {self.timeout}s",
                duration_seconds=duration,
            )

        duration = time.monotonic() - start
        logger.debug(
            "HarnessAgent finished exit_code={} duration={:.1f}s",
            result.returncode, duration,
        )

        stdout = result.stdout
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0

        if self.backend == HarnessBackend.CLAUDE_CODE:
            stdout, input_tokens, output_tokens, total_tokens = (
                _parse_claude_code_json(result.stdout)
            )
        elif self.backend == HarnessBackend.CODEX:
            stdout, input_tokens, output_tokens, total_tokens = (
                _parse_codex_jsonl(result.stdout)
            )

        return HarnessRunResult(
            exit_code=result.returncode,
            stdout=stdout,
            stderr=result.stderr,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def close(self) -> None:
        if self._xdg_path.exists():
            shutil.rmtree(self._xdg_path, ignore_errors=True)

    # ------------------------------------------------------------------
    # Backend-specific setup
    # ------------------------------------------------------------------

    def _setup_backend(self, endpoint: Endpoint) -> None:
        api_key = endpoint.api_key
        base_url = endpoint.base_url

        if self.backend == HarnessBackend.CLAUDE_CODE:
            self._setup_claude_code(api_key, base_url)
        elif self.backend == HarnessBackend.CODEX:
            self._setup_codex(api_key, base_url, endpoint.provider)
        elif self.backend == HarnessBackend.OPENCODE:
            self._setup_opencode(api_key, base_url, endpoint.provider)

    def _setup_claude_code(self, api_key: str, base_url: str) -> None:
        self._env["ANTHROPIC_API_KEY"] = api_key
        self._env["ANTHROPIC_BASE_URL"] = base_url
        self._env["ANTHROPIC_MODEL"] = self.model


    def _setup_codex(self, api_key: str, base_url: str, provider: str) -> None:
        self._env["OPENAI_API_KEY"] = api_key
        self._env["GRAFTING_CODEX_API_KEY"] = api_key
        self._env["OPENAI_BASE_URL"] = base_url
        self._env["OPENAI_MODEL"] = self.model

        wire_api = "responses"

        codex_dir = self._xdg.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_lines = [
            f'model = "{self.model}"',
            'model_provider = "grafting-managed"',
            "",
            "[model_providers.grafting-managed]",
            '"name" = "Grafting Managed Provider"',
            f'base_url = "{base_url}"',
            f'wire_api = "{wire_api}"',
            'env_key = "GRAFTING_CODEX_API_KEY"',
            "",
            "[features]",
            "codex_hooks = false",
        ]
        (codex_dir / "config.toml").write_text(
            "\n".join(config_lines) + "\n", encoding="utf-8",
        )

    def _setup_opencode(self, api_key: str, base_url: str, provider: str) -> None:
        self._env["GRAFTING_OPENCODE_API_KEY"] = api_key
        self._env["GRAFTING_OPENCODE_BASE_URL"] = base_url
        self._env["GRAFTING_OPENCODE_MODEL"] = self.model
        options: dict[str, str] = {"baseURL": base_url, "apiKey": api_key}

        if uses_responses_api(provider):
            npm_package = "@ai-sdk/openai"
        else:
            npm_package = "@ai-sdk/openai-compatible"

        provider_key = "grafting-managed"
        provider_name = "Grafting Managed Provider"
        config_payload = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                provider_key: {
                    "npm": npm_package,
                    "name": provider_name,
                    "options": options,
                    "models": {self.model: {"name": self.model}},
                }
            },
        }
        model_state = {
            "recent": [{"providerID": provider_name, "modelID": self.model}],
            "favorite": [],
            "variant": {},
        }

        config_path = self._xdg.config / "opencode" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        auth_path = self._xdg.data / "opencode" / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text("{}", encoding="utf-8")

        model_path = self._xdg.state / "opencode" / "model.json"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(
            json.dumps(model_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _refresh_opencode_config_key(self, api_key: str) -> None:
        config_path = self._xdg.config / "opencode" / "config.json"
        if not config_path.exists():
            return
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        for prov in cfg.get("provider", {}).values():
            opts = prov.get("options", {})
            if "apiKey" in opts:
                opts["apiKey"] = api_key
        config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI output parsers for token usage extraction
# ---------------------------------------------------------------------------

def _parse_claude_code_json(stdout: str) -> tuple[str, int, int, int]:
    """Parse Claude Code --output-format=json stdout.

    Sums all input token types (input + cache_read + cache_creation)
    because Claude Code reports non-cached input separately from cached.

    Returns (text_content, input_tokens, output_tokens, total_tokens).
    On parse failure, returns (original_stdout, 0, 0, 0).
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout, 0, 0, 0

    text = data.get("result", stdout)
    usage = data.get("usage", {})
    inp = (
        (usage.get("input_tokens", 0) or 0)
        + (usage.get("cache_read_input_tokens", 0) or 0)
        + (usage.get("cache_creation_input_tokens", 0) or 0)
    )
    out = usage.get("output_tokens", 0) or 0
    return text, inp, out, inp + out


def _parse_codex_jsonl(stdout: str) -> tuple[str, int, int, int]:
    """Parse Codex --json JSONL event stream.

    Sums input_tokens/output_tokens from turn.completed events.
    Returns (text_content, input_tokens, output_tokens, total_tokens).
    On parse failure, returns (original_stdout, 0, 0, 0).
    """
    inp = 0
    out = 0
    text = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        event_type = event.get("type", "")
        if event_type == "turn.completed":
            usage = event.get("usage", {})
            inp += usage.get("input_tokens", 0) or 0
            out += usage.get("output_tokens", 0) or 0
        elif event_type == "message":
            text = event.get("content", text)
    if not text:
        text = stdout
    return text, inp, out, inp + out
