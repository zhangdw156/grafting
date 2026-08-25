---
name: tool-graph-exploration
description: Analyze the environment's tool graph to sample subgraphs for task generation. Each subgraph corresponds to one demand. Checks existing task coverage in tasks.sqlite3 to prefer under-covered tools. Adapts sampling to the current rule and persona.
---

# Tool Graph Exploration

Analyze the environment's tool graph and sample a set of subgraphs
that will each become one demand in the generated task. This is the
first step in task generation — all subsequent skills operate on the
subgraphs you produce here.

## Inputs

- `env/spec/tool_graph.json` — full tool relationship graph
- `env/spec/tools.jsonl` — tool definitions with categories
- `tasks.sqlite3` — existing task pool (query for coverage)
- The current **rule** and **persona** from the entry prompt

## Process

### 1. Understand the Tool Graph

Read `tool_graph.json`. Identify:

- **Mutation tools** (category: "mutation") — these are the demand
  anchors. Every subgraph must contain at least one mutation tool.
- **Query tools** (category: "query") — these support mutations by
  providing required IDs or data.
- **Data dependency edges** — tool A's output feeds tool B's input.
- **State precondition edges** — tool B requires tool A to have been
  called first.

### 2. Check Existing Coverage

Query `tasks.sqlite3` to understand which tools have already been
covered by existing tasks:

```
sqlite3 tasks.sqlite3 "SELECT task_id, task_file FROM tasks WHERE env_name='<env>' AND status='succeeded'"
```

For each existing task, read its demands and infer which mutation
tools it exercises. Build a coverage map:

| Mutation Tool | Times Covered |
|---------------|---------------|
| update_X      | 5             |
| delete_X      | 0             |
| create_X      | 3             |

Prefer sampling mutation tools with low coverage.

### 3. Sample Subgraphs

Each subgraph = one mutation tool (the anchor) + its prerequisite
query tools (follow predecessor edges backward).

The subgraphs collectively define one complex demand. Target
**3-5 mutation tools** total to produce a demand that naturally
requires 2-5 user turns (average ~3) to complete.

| Rule | Mutation Tools | Guidance |
|------|---------------|----------|
| normal | 3-5 | Standard complexity, multiple related operations |
| miss_func | 3-4 | Plus 1-2 sub-goals for non-existent capabilities |
| miss_param | 3-5 | Same count, user will be vague about parameters |
| error_recovery | 3-4 | At least one tool whose preconditions can be violated |
| long_context | 5-7 | Broader coverage across the environment |

When sampling subgraphs:

- **Prefer connected subgraphs** — subgraphs that share nodes
  (e.g., both need `get_user_details`) create a coherent user story.
- **Include dependency chains** — not just parallel mutations,
  but sequential ones where one operation depends on another's result.
- **Avoid identical subgraphs** — each subgraph should have a
  different mutation anchor.

### 4. Output

Produce a structured list of subgraphs. For each subgraph:

```
Subgraph 1:
  Anchor: update_reservation_flights (mutation)
  Prerequisites: find_user_by_name → get_user_details → get_reservation_details → search_direct_flight
  Data flow: name → user_id → reservation_id → flight_number → update

Subgraph 2:
  Anchor: update_reservation_baggages (mutation)
  Prerequisites: get_reservation_details
  Data flow: reservation_id → update (shares reservation_id with subgraph 1)
```

This list is your working artifact for the next skill
(task-creation). You do NOT write it to a file — carry it forward
in your working context.

## Key Principles

- Every mutation tool in the graph should eventually be covered
  across many tasks. Prioritize the least-covered ones.
- Subgraph complexity should match the persona's expertise — a
  novice persona gets simpler chains, an expert gets deeper ones.
- The subgraphs define WHAT the task covers. HOW demands are
  phrased is the job of task-creation.
