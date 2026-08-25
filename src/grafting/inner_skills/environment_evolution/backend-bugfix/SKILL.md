---
name: backend-bugfix
description: Fix bugs in runtime/backend.py identified by assess-diagnosis. Handles INTERNAL_ERROR crashes from parameter mismatches, SQL errors, missing handler methods, and ID generation bugs. Applies targeted fixes following the environment contract.
---

# Backend Bugfix

Given specific error traces from assess-diagnosis, locate and fix bugs in backend.py.

## Inputs

- `output/diagnosis.json` — structured diagnosis from assess-diagnosis
- `env/runtime/backend.py` — backend implementation to fix
- `env/spec/tools.jsonl` — authoritative tool definitions
- `env/world/schema.sql` — database schema
- `env/world/world.db` — seed database

## Common Bug Patterns

### Pattern 1: Parameter name mismatch
tools.jsonl defines `inputSchema` with property `departure_city` but backend.py handler takes `origin_city`. The `**arguments` unpacking raises TypeError.

**Fix**: Rename backend.py parameter to match tools.jsonl. tools.jsonl is authoritative for parameter names.

### Pattern 2: ID prefix mismatch
`_next_id("text_materials", "material_id", "TM")` but existing IDs use prefix `TXT` (e.g., `TXT0000001`). Parsing `last[len("TM"):]` yields `"T0000001"` which `int()` cannot parse.

**Fix**: Change the prefix argument to match actual ID prefixes in world.db. Query the table to check existing ID format.

### Pattern 3: SQL column name mismatch
backend.py queries `SELECT user_name FROM users` but schema.sql defines the column as `username`.

**Fix**: Update SQL in backend.py to match schema.sql column names exactly.

### Pattern 4: Foreign key schema error
Table schema has `FOREIGN KEY (business_id) REFERENCES "_mig"(business_id)` — referencing a non-existent table (synthesis artifact from table rename).

**Fix**: Rebuild the table with correct FK reference. This requires: dump data → drop table → create with fixed FK → re-insert data. Use schema.sql as the authoritative source for correct table names.

### Pattern 5: Missing tool handler
`_TOOL_NAMES` includes `search_products` but no `_tool_search_products` method exists.

**Fix**: Implement the handler following the pattern of similar tools in the same backend. Read tools.jsonl for the expected parameters and return format.

## Process

1. Read `output/diagnosis.json` for the list of issues
2. For each issue:
   a. Locate the relevant code in backend.py
   b. Cross-reference against tools.jsonl and schema.sql
   c. Apply the minimal fix — do not rewrite working code
3. After all fixes, verify:
   - `_TOOL_NAMES` matches tool names in tools.jsonl exactly
   - All `_tool_*` method signatures match tools.jsonl `inputSchema` properties
   - All SQL table/column references match schema.sql

## Constraints

- Follow the backend implementation rules in `references/contract.md`
- Do not change the public interface (`call`, `visible_state`, `__init__`)
- Do not add imports beyond the standard library
- Do not introduce randomness or non-determinism
- `call()` must still never raise — all errors returned as structured dicts

## Output

- Modified `env/runtime/backend.py` in place
- Write `output/fixes_applied.json`:

```json
{
  "fixes": [
    {
      "file": "env/runtime/backend.py",
      "pattern": "id_prefix_mismatch",
      "tool": "add_text",
      "change": "Changed _next_id prefix from 'TM' to 'TXT' on line 405"
    }
  ]
}
```
