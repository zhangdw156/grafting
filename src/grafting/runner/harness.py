from __future__ import annotations

import itertools
import shutil
from pathlib import Path

from grafting.driver import HarnessBackend
from grafting.runner.agent import HarnessAgent
from grafting.runner.base import BaseRunner
from grafting.runner.types import HarnessRunResult, MCPConfig
from grafting.utils import Endpoint, logger


class BaseHarnessRunner(BaseRunner):

    env_dir: Path

    def __init__(
        self,
        *,
        output_root: Path,
        workers: int = 1,
        backend: HarnessBackend,
        project_root: Path,
        endpoint: Endpoint,
        inner_skills_root: Path,
        sandbox_path: Path | None = None,
        timeout: int = 600,
    ) -> None:
        super().__init__(output_root=output_root, workers=workers)
        self.backend = backend
        self.project_root = project_root
        self.inner_skills_root = inner_skills_root.resolve()
        self.sandbox_path = sandbox_path
        self.timeout = timeout
        self.endpoint = endpoint
        self._sandboxes: list[Path] = []
        self._agents = [
            HarnessAgent(
                backend=self.backend,
                endpoint=self.endpoint,
                project_root=self.project_root,
                xdg_path=self._agent_xdg_path(index),
                timeout=self.timeout,
            )
            for index in range(self.workers)
        ]
        self._agent_cycle = itertools.cycle(range(len(self._agents)))
        logger.info(
            "Using endpoint: model={} provider={} workers={}",
            endpoint.model,
            endpoint.provider,
            workers,
        )

    def _agent_xdg_path(self, index: int) -> Path:
        if self.sandbox_path is not None:
            return self.sandbox_path / f"agent-{index:04d}"
        return self.output_root / ".agents" / f"agent-{index:04d}"

    def run_agent(
        self,
        worker_index: int,
        prompt: str,
        *,
        mcp: list[MCPConfig] | None = None,
    ) -> HarnessRunResult:
        return self._with_provenance(
            self._agents[worker_index].run(prompt, mcp=mcp),
            self._agents[worker_index],
        )

    def run_prompt(
        self,
        prompt: str,
        *,
        mcp: list[MCPConfig] | None = None,
    ) -> HarnessRunResult:
        index = next(self._agent_cycle)
        agent = self._agents[index]
        return self._with_provenance(agent.run(prompt, mcp=mcp), agent)

    def shutdown(self) -> None:
        for agent in self._agents:
            agent.close()
        self._agents.clear()

    def _with_provenance(
        self,
        result: HarnessRunResult,
        agent: HarnessAgent,
    ) -> HarnessRunResult:
        return HarnessRunResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            harness_backend=self.backend.value,
            model=agent.model,
            provider=agent.provider,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )

    # -- sandbox utilities (shared by all subclasses) -----------------------

    def _pre_clean_sandbox(
        self,
        sandbox_path: Path | None,
        output_root: Path | None,
    ) -> None:
        """Remove leftover sandbox project/ dirs from previous runs."""
        root = Path(sandbox_path) if sandbox_path else (Path(output_root or "results") / ".agents")
        if not root.exists():
            return
        for d in root.iterdir():
            if d.is_dir():
                project = d / "project"
                if project.exists():
                    shutil.rmtree(project, ignore_errors=True)
                    logger.debug("Cleaned stale sandbox: {}", project)

    _NATIVE_SKILL_DIRS = {
        HarnessBackend.CLAUDE_CODE: ".claude/skills",
        HarnessBackend.CODEX: ".codex/skills",
        HarnessBackend.OPENCODE: ".opencode/skills",
    }

    def _native_skills_prefix(self) -> str:
        return self._NATIVE_SKILL_DIRS[self.backend]

    def _setup_harness_skills(self, project: Path) -> None:
        """Copy inner skills into the harness-native skill directory."""
        if not self.inner_skills_root.exists():
            return

        skill_dirs = [
            d for d in self.inner_skills_root.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        ]

        native_dir = project / self._native_skills_prefix()
        native_dir.mkdir(parents=True, exist_ok=True)
        for skill_dir in skill_dirs:
            dest = native_dir / skill_dir.name
            if dest.exists() or dest.is_symlink():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)

    def _repoint_agents_to_sandboxes(self) -> None:
        """Update each agent's project_root to point to its sandbox."""
        for i in range(self.workers):
            self._agents[i].project_root = self._sandboxes[i]

    def _clean_sandbox_output(self, sandbox: Path) -> None:
        output = sandbox / "output"
        if output.exists():
            shutil.rmtree(output)
        output.mkdir(parents=True, exist_ok=True)

    def _link_env(self, sandbox: Path, env_name: str) -> None:
        env_copy = sandbox / "env"
        if env_copy.exists():
            shutil.rmtree(env_copy)
        target = self.env_dir / env_name
        if target.exists():
            shutil.copytree(target, env_copy)
