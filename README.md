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
# 1. Synthesize executable environments from agent skill packages.
uv run grafting.env.synthesis \
  raw_skills_root=/path/to/skills \
  env_output=outputs/environments

# 2. Generate grounded tasks.
uv run grafting.task.generate \
  env_dir=outputs/environments

# 3. Simulate multi-turn trajectories.
uv run grafting.sim \
  env_dir=outputs/environments \
  task_dir=results/tasks

# 4. Validate completed trajectories.
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

## 🔬 Training and Evaluation

The source checkout includes the paper-specific external integration files:

```text
repro/
├── verl/
│   └── fathom_reward.py
└── gorilla/
    ├── fathom_fc.py
    └── register_fathom.patch
```

### Prepare the veRL training environment

Training uses a separate veRL environment from the Python 3.13 environment
used by the Grafting pipeline. Use veRL `v0.7.1`, commit
`bec9ef74768dd201881cd4e54cd0385e87caae27`, and follow its upstream
installation instructions for your hardware:

```bash
git clone https://github.com/verl-project/verl.git
cd verl
git checkout bec9ef74768dd201881cd4e54cd0385e87caae27
```

Point veRL's custom reward configuration at the absolute path of the released
reward file:

```yaml
reward:
  custom_reward_function:
    path: /absolute/path/to/grafting/repro/verl/fathom_reward.py
    name: compute_score
```

The two Fathoming rounds use the same GRPO configuration:

| Hyperparameter | Value |
|---|---:|
| Base model | `Qwen3-4B-Instruct-2507` |
| Learning rate | `2e-6` |
| Global batch size | `128` |
| GRPO group size | `16` |
| KL coefficient | `0.001` |
| Epochs | `1` |
| Maximum sequence length | `30,720` |

### Prepare the BFCLv4 evaluation environment

The BFCL integration is pinned to the official Gorilla commit
`6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`. The patch adds the six Fathom
handler imports and model registrations.

```bash
export GRAFTING_ROOT=/absolute/path/to/grafting

git clone https://github.com/ShishirPatil/gorilla.git
cd gorilla
git checkout 6ea57973c7a6097fd7c5915698c54c17c5b1b6c8

cp "$GRAFTING_ROOT/repro/gorilla/fathom_fc.py" \
  berkeley-function-call-leaderboard/bfcl_eval/model_handler/local_inference/
git apply "$GRAFTING_ROOT/repro/gorilla/register_fathom.patch"

python -m venv .venv
source .venv/bin/activate
python -m pip install -e berkeley-function-call-leaderboard
```

The registered model names cover whether the Fathom system prompt is enabled
and which thinking prefix is appended before generation:

| Model name | System prompt | Exact generation prefix |
|---|---|---|
| `fathom-sp-noprefix` | Fathom | `""` |
| `fathom-sp-emptythink` | Fathom | `"<think>\n\n</think>\n\n"` |
| `fathom-sp-openthink` | Fathom | `"<think>\n"` |
| `fathom-nosp-noprefix` | Upstream Qwen | `""` |
| `fathom-nosp-emptythink` | Upstream Qwen | `"<think>\n\n</think>\n\n"` |
| `fathom-nosp-openthink` | Upstream Qwen | `"<think>\n"` |

Select the variant that matches the model's training prompt and thinking mode,
then serve the trained checkpoint through an OpenAI-compatible vLLM endpoint:

```bash
export FATHOM_BFCL_MODEL=fathom-sp-noprefix

vllm serve /path/to/model \
  --served-model-name "$FATHOM_BFCL_MODEL" \
  --trust-remote-code
```

In the BFCL environment, point the handler at the running endpoint and the
tokenizer used by the checkpoint:

```bash
export REMOTE_OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export REMOTE_OPENAI_API_KEY=EMPTY
export REMOTE_OPENAI_TOKENIZER_PATH=/path/to/tokenizer-or-base-model

bfcl generate \
  --model "$FATHOM_BFCL_MODEL" \
  --test-category multi_turn \
  --backend vllm \
  --skip-server-setup \
  --num-threads 32 \
  --result-dir results/bfcl

bfcl evaluate \
  --model "$FATHOM_BFCL_MODEL" \
  --test-category multi_turn \
  --result-dir results/bfcl \
  --score-dir scores/bfcl
```

The paper reports the mean over five independent BFCLv4 evaluation runs.

## 📄 License

The Grafting codebase is released under the [MIT License](LICENSE).
