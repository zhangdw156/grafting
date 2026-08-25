---
name: task-mutation
description: Rewrite a task that failed conflict check or validation. Changes demands, entities, world_setup, or persona framing while preserving the same tool coverage intent.
---

# Task Mutation

When task-pool-conflict-check detects a duplicate, or when
validation checks fail and simple fixes don't resolve the issue,
mutate the task to produce a structurally different variant.

## Mutation Strategies

Apply one or more of these strategies, then re-run all validation
checks (grounding, solvability, clarity, world-setup-validation,
conflict-check).

### 1. Change Target Entities

Pick different concrete entities from world.db:

- Different user (different name, different data)
- Different reservation/order/account
- Different files or directories

This changes what the demands reference without changing the tool
coverage.

### 2. Reframe Demands

Rewrite demand text to describe a different scenario that exercises
the same mutation tool:

- Original: "Change flight from May 17 to May 16"
- Mutated: "Upgrade the cabin class from economy to business"

Both exercise `update_reservation_flights` but describe different
scenarios.

### 3. Swap Subgraphs

Go back to tool-graph-exploration and sample different mutation
anchors. This changes which tools the task covers entirely.

### 4. Adjust world_setup

Change the initial state to create a different scenario:

- Different initial balance/inventory
- Different constraints or preconditions
- Different session context

### 5. Modify Persona

Adjust `user_agent_config` to frame the same operations from a
different perspective:

- Different role (manager vs. customer vs. analyst)
- Different knowledge level (knows more or less)
- Different personality traits

## Process

1. Choose 1-2 mutation strategies based on why the conflict
   occurred:
   - Same entities? → Strategy 1
   - Same scenario? → Strategy 2
   - Same tools? → Strategy 3
   - Same initial state? → Strategy 4

2. Apply the mutations to produce a new task.json draft

3. Re-run ALL validation checks:
   - task-grounding-check
   - task-solvability-check
   - task-demand-clarity-check
   - world-setup-validation
   - task-pool-conflict-check

4. If still conflicting after 3 attempts, report failure.
   The subgraph space for this environment may be saturated.

## Output

Overwrite `output/task.json` with the mutated version.
