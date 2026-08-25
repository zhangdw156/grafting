---
name: assess-diagnosis
description: Read assess artifacts (report.json, trajectories, evaluations) and classify rejections into actionable categories. Determines whether issues are environment bugs, contract mismatches, data problems, or assistant limitations. Produces a structured diagnosis that guides which fix skill to apply.
---

# Assess Diagnosis

Read the assess artifacts and produce a structured diagnosis of environment quality issues.

## Inputs

- `assess_artifacts/report.json` — assessment summary with verdict counts
- `assess_artifacts/trajectories/<env_name>/traj_*.json` — full trajectory files
- `assess_artifacts/evaluations/<env_name>/eval_*.json` — evaluation results
- `assess_artifacts/evaluations/evaluations.sqlite3` — evaluations database
- `env/spec/tools.jsonl` — tool definitions
- `env/runtime/backend.py` — backend implementation
- `env/world/schema.sql` — database schema

## Diagnosis Taxonomy

### Category 1: Environment Bug (`environment_bug`)

**Signature**: Tool calls return `{"success": false, "error": {"code": "INTERNAL_ERROR", ...}}`

**Root causes**:
- `TypeError` from `**arguments` unpacking — parameter name in tools.jsonl does not match backend.py handler signature
- `KeyError` / `AttributeError` in tool handler — missing column, wrong dict key
- SQL errors — referencing non-existent table or column, FK constraint violations
- ID generation errors — `_next_id` prefix mismatch with actual data

**Action**: Use `backend-bugfix` skill

### Category 2: Contract Mismatch (`contract_mismatch`)

**Signature**: No INTERNAL_ERROR, but tool calls produce unexpected results or are never called because tools.jsonl describes them differently than backend.py implements them.

**Root causes**:
- tools.jsonl `inputSchema` property names differ from backend.py handler parameter names
- tool_graph.json node names differ from tools.jsonl tool names
- tools.jsonl `category` differs from tool_graph.json node `category`
- `_TOOL_NAMES` frozenset in backend.py is missing tools or has extras

**Action**: Use `contract-alignment` skill

### Category 3: Data Issue (`data_issue`)

**Signature**: Tool calls succeed but return empty results; auth tools fail because user credentials in world_setup don't match seed data; too few records for meaningful tasks.

**Root causes**:
- Dangling foreign keys in world.db (referencing non-existent rows or tables)
- Empty or near-empty tables that tools query
- Missing status diversity (all orders in same state)
- Schema FK references a renamed or non-existent table

**Action**: Use `seed-data-enrichment` skill (for data sparsity) or `backend-bugfix` (for FK schema errors)

### Category 4: Assistant Limitation (`assistant_limitation`)

**Signature**: `demand_not_fulfilled` WITHOUT any INTERNAL_ERROR. All tool calls succeed, but the assistant fails to complete all steps of the demand.

**Indicators**:
- Tool call success rate is high (>90%)
- No INTERNAL_ERROR or UNKNOWN_TOOL errors
- Assistant simply stops short or makes wrong choices
- Conversation is long (many turns) but incomplete

**Action**: **No fix needed** — this is a model capability issue, not an environment defect. Mark as pass.

### Category 5: Too Simple (`too_simple`)

**Signature**: All trajectories accepted (5/5) with no issues.

**Action**: Use `seed-data-enrichment` skill to add complexity.

## Process

1. **Read report.json** — extract accepted/rejected counts and rejection reason distribution
2. **Scan each trajectory file** — for every `role: "function"` message, check if content contains `"INTERNAL_ERROR"`, `"UNKNOWN_TOOL"`, or `"AUTH_FAILED"`
3. **For each error found**, extract:
   - Tool name (`name` field of the function message)
   - Error code and message
   - The assistant's tool call arguments (from the preceding assistant message)
4. **Cross-reference with tools.jsonl** — for INTERNAL_ERROR from TypeError, compare parameter names in tools.jsonl inputSchema against backend.py handler signatures
5. **Query evaluations.sqlite3** — count verdict patterns, check which dimensions failed (demand_fulfilled, assistant_grounded, user_consistent)
6. **Classify** using the taxonomy above. If multiple categories apply, prioritize: environment_bug > contract_mismatch > data_issue > assistant_limitation

## Output

Write `output/diagnosis.json`:

```json
{
  "category": "environment_bug",
  "confidence": "high",
  "summary": "3/5 trajectories hit INTERNAL_ERROR in add_text tool due to ID prefix mismatch",
  "issues": [
    {
      "type": "internal_error",
      "tool_name": "add_text",
      "detail": "TypeError: invalid literal for int() with base 10: 'T0000010'",
      "evidence": "traj_003.json, traj_007.json, traj_011.json",
      "suggested_fix": "Change _next_id prefix from 'TM' to 'TXT' to match existing IDs"
    }
  ],
  "recommended_skills": ["backend-bugfix"],
  "pass_without_fix": false
}
```
