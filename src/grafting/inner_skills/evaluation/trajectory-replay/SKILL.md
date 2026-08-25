---
name: trajectory-replay
description: Replay all tool calls from a trajectory against a fresh environment copy. Produces replay_result.json with per-call comparison and final DB state available for downstream verification.
---

# Trajectory Replay

Replay the trajectory's tool calls against a fresh copy of the
environment database. This produces deterministic ground truth
about what actually happened in the database — used by
demand-verification to check whether goals were achieved.

## Process

### 1. Run the Replay Script

```bash
python scripts/replay_trajectory.py \
  --trajectory trajectory.json \
  --env-dir env \
  --output output/replay_result.json
```

This will:
1. Copy `env/world/world.db` to `output/eval.db`
2. Execute the task's `world_setup` SQL against `eval.db`
3. Load `EnvironmentBackend` from `env/runtime/backend.py`
4. Extract all tool calls from the trajectory messages
5. Replay each call via `backend.call(tool_name, arguments)`
6. Compare each replay result against the original result
7. Write `output/replay_result.json`

### 2. Read the Replay Result

The output file contains:

```json
{
  "calls": [
    {
      "index": 0,
      "tool_name": "find_user_id_by_name",
      "arguments": {"first_name": "Emma", "last_name": "Kim"},
      "original_result": {"success": true, "data": ...},
      "replay_result": {"success": true, "data": ...},
      "match": true
    },
    ...
  ],
  "total": 8,
  "matched": 8,
  "mismatched": 0,
  "eval_db": "output/eval.db"
}
```

### 3. Check for Mismatches

The replay script automatically handles **token-based mismatches**:
when an auth tool returns a different token during replay (due to
timestamp differences), subsequent tool calls are patched with the
new token. The `token_substitutions` field in the output shows how
many calls were patched.

If `mismatched > 0` after token substitution, inspect the remaining
mismatched calls. Possible causes:
- Non-deterministic backend behavior (timestamps, random IDs)
- Corrupted trajectory data
- Backend bug that produces different results on repeated calls

Note that minor mismatches (e.g., different timestamps) do not
automatically invalidate the trajectory. Use judgment to determine
whether the mismatch affects evaluation.

### 4. Proceed to Demand Verification

After replay, `output/eval.db` contains the final database state.
Use this database in the demand-verification skill to verify
whether the demand's goals were achieved.
