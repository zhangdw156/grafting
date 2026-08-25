<div align="center">

<h1>🌱 Grafting</h1>

<h3>From agent skills to executable environments and verified tool-use trajectories</h3>

<p>
  <img src="https://img.shields.io/badge/EMNLP-2026%20Main-6f42c1" alt="EMNLP 2026 Main">
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/License-MIT-blue" alt="MIT License">
</p>

<p>
  <a href="https://huggingface.co/datasets/zhangdw/grove-500"><b>Grove-500</b></a>
  &nbsp;·&nbsp;
  <a href="https://huggingface.co/datasets/zhangdw/grove-5k"><b>Grove-5K</b></a>
  &nbsp;·&nbsp;
  <a href="https://huggingface.co/datasets/zhangdw/fathom-12k"><b>Fathom-12K</b></a>
</p>

</div>

---

Grafting is the implementation accompanying
**From Agent Skills to Executable Environments: Frontier-Targeted Training for
Tool-Use LLMs**. It turns workflow-bearing agent skills into self-contained
executable environments, generates grounded tasks and multi-turn trajectories,
and validates those trajectories against fresh environment state.

## 🧭 Pipeline

<p align="center">
  <img src="assets/pipeline.png" alt="Overview of the Grafting and Fathoming pipeline" width="100%">
</p>

## 📦 At a Glance

| Artifact | Contents | Role |
|---|---|---|
| **Grafting** | Environment, task, simulation, validation, and Qwen3 export code | Implementation |
| **[Grove-500](https://huggingface.co/datasets/zhangdw/grove-500)** | 500 executable environments | End-to-end experiments |
| **[Grove-5K](https://huggingface.co/datasets/zhangdw/grove-5k)** | 5,101 executable environments | Synthesis-scale release |
| **[Fathom-12K](https://huggingface.co/datasets/zhangdw/fathom-12k)** | 12,745 verified trajectories | Verified trajectory pool |

## 🚀 Quick Start

### Requirements

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- At least one supported model endpoint
- One supported coding-agent CLI—Claude Code, Codex, or OpenCode—for
  environment synthesis, task generation, evaluation, and evolution

### Install

```bash
git clone https://github.com/zhangdw156/grafting.git
cd grafting
uv sync --frozen
cp settings.yaml.example settings.yaml
```

Edit `settings.yaml` with your endpoint URLs, API keys, and model names.

### Run the pipeline

```bash
# 1. Start from an existing directory of agent skill packages.
find /path/to/skills -name SKILL.md | head

# 2. Synthesize executable environments from those skills.
uv run grafting.env.synthesis \
  raw_skills_root=/path/to/skills \
  env_output=outputs/environments

# 3. Generate grounded tasks.
uv run grafting.task.generate \
  env_dir=outputs/environments

# 4. Simulate multi-turn trajectories.
uv run grafting.sim \
  env_dir=outputs/environments \
  task_dir=results/tasks

# 5. Validate completed trajectories.
uv run grafting.eval \
  env_dir=outputs/environments \
  traj_dir=results/trajectories
```

## 🧭 Main Commands

| Command | Purpose |
|---|---|
| `grafting.env.synthesis` | Build self-contained executable environments |
| `grafting.env.evolve` | Repair or enrich environments using assessment evidence |
| `grafting.task.generate` | Generate grounded tasks from tool graphs and world state |
| `grafting.sim` | Simulate multi-turn user-assistant interactions |
| `grafting.eval` | Replay and validate trajectories |
| `grafting.export.qwen3` | Export accepted trajectories in Qwen3 SFT format |

## 🧱 Environment Package

Every generated environment follows the same portable layout:

```text
<environment_id>/
├── spec/
│   ├── ENVIRONMENT.md
│   ├── tool_graph.json
│   └── tools.jsonl
├── runtime/
│   └── backend.py
├── tests/
│   └── test_runtime.py
└── world/
    ├── schema.sql
    └── world.db
```

The backend is deterministic and stores all mutable state in the supplied
SQLite database. Downstream simulation and validation use fresh database
copies, preserving the released seed world.

## 📄 License

The Grafting codebase is released under the [MIT License](LICENSE).
