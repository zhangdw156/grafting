---
name: task-pool-conflict-check
description: Compare the generated task against existing tasks in tasks.sqlite3 to detect duplicates or near-duplicates. Triggers task-mutation when a conflict is found.
---

# Task Pool Conflict Check

Check whether the generated task is too similar to an existing task
in the pool for the same environment.

## Process

### 1. Load Existing Tasks

Query tasks.sqlite3 for the current environment:

```bash
sqlite3 tasks.sqlite3 "SELECT task_id, task_file FROM tasks WHERE env_name='<env>' AND status='succeeded'"
```

For each existing task, read its task file and extract:
- demands (list of goal strings)
- the set of mutation tools each demand implies

### 2. Compare

Check for conflicts at two levels:

**Structural similarity**: Do the new and existing tasks cover
the same mutation tools?

- Extract the mutation tool set from each task's demands (infer
  from the subgraphs you sampled)
- If two tasks have identical mutation tool sets, flag as
  potential conflict

**Semantic similarity**: Are the demand texts describing the
same scenarios?

- Compare demand texts pairwise (keyword overlap, entity overlap)
- If most demands describe the same actions on the same entities,
  flag as conflict

### 3. Decision

| Structural | Semantic | Verdict |
|-----------|----------|---------|
| Different | — | No conflict |
| Same | Different | No conflict (same tools, different scenarios) |
| Same | Similar | **CONFLICT** → trigger task-mutation |

### 4. Report

```
Existing task: tau2_airline/task_003
  Mutation tools: {update_reservation_flights, update_reservation_baggages}
  Demands: ["Change flight to May 18", "Add 1 checked bag"]

New task:
  Mutation tools: {update_reservation_flights, update_reservation_baggages}
  Demands: ["Change flight to May 16", "Add 2 checked bags"]

Verdict: SAME tools, SIMILAR demands → CONFLICT
Action: trigger task-mutation
```

## Notes

- A conflict does not mean the task is bad — it means the pool
  already has a sufficiently similar task.
- After task-mutation rewrites the task, re-run this check.
- After K failed mutation attempts (default: 3), report failure
  and move on. The subgraph space for this environment may be
  exhausted.
