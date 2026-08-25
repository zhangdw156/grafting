from __future__ import annotations

from enum import StrEnum


class HarnessBackend(StrEnum):
    """Supported harness CLI backends.

    EXTEND: adding a new harness backend requires changes in:
      1. Here: add enum value
      2. Here: add match branch in build_harness_command()
      3. runner/agent.py: add _setup_xxx() + build_xxx() class method
      4. runner/harness.py: add builder mapping in _create_pool_agents()
      5. runner/environment.py: add harness_skill_dirs entry in _setup_harness_skills()
    """

    OPENCODE = "opencode"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


def build_harness_command(
    backend: HarnessBackend,
    task_text: str,
    *,
    binary_override: str | None = None,
    model: str = "",
) -> list[str]:
    """Build the subprocess argv for the given harness backend."""
    match backend:
        case HarnessBackend.OPENCODE:
            binary = binary_override or "opencode"
            cmd = [binary, "run", "--dangerously-skip-permissions"]
            if model:
                cmd.extend(["--model", model])
            cmd.append(task_text)
            return cmd
        case HarnessBackend.CLAUDE_CODE:
            binary = binary_override or "claude"
            return [
                binary,
                "-p",
                task_text,
                "--output-format=json",
                "--dangerously-skip-permissions",
            ]
        case HarnessBackend.CODEX:
            binary = binary_override or "codex"
            return [
                binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-c",
                "features.codex_hooks=false",
                "-c",
                'model_reasoning_effort="medium"',
                "--dangerously-bypass-approvals-and-sandbox",
                task_text,
            ]
