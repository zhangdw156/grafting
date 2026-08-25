---
name: quality-validation
description: Validate a completed environment package for quality, consistency, and data volume. Combines tool schema quality checks (MCP description best practices, enum/required consistency, security patterns), cross-artifact consistency checks (tool names match across files, table names valid, test coverage), and seed data volume checks (total rows, per-table minimums, user count, mutation scenario coverage). Run this after all environment files are generated. Produces a structured JSON quality report.
---

# Quality Validation

You are the final quality gate before an environment is registered. Read all 7 files in the environment package and run three sets of checks:

1. **Tool schema quality** — validate each tool in tools.jsonl against MCP best practices
2. **Cross-artifact consistency** — validate that all files are consistent with each other
3. **Seed data volume** — validate that world.db has enough rows to support diverse task generation

## Sandbox Paths

Read all files from `output/<env_name>/` to validate.

## Input

Read from `output/<env_name>/`:
- `spec/tools.jsonl`
- `spec/tool_graph.json`
- `spec/ENVIRONMENT.md`
- `runtime/backend.py`
- `world/schema.sql`
- `world/world.db`
- `tests/test_runtime.py`

## Part 1: Tool Schema Quality

Read `spec/tools.jsonl`. For every tool, run the checks in `references/schema_quality_checks.md`.

Summary of check categories:
- **A1-A8**: Tool-level description (core function, when to/not to use, return values, errors, prerequisites, sentence structure, no prohibited content)
- **B1-B8**: Parameter-level description (purpose, required/optional, format, enums, ranges, forbidden values, dependencies, examples)
- **C1-C2**: Category validation (valid value, behavior consistency)
- **D1-D3**: Schema structure (enum matches description, required matches description, no undocumented params)
- **E1-E3**: Cross-tool consistency (unified return format, repeated param consistency, security)

## Part 2: Cross-Artifact Consistency

Run these checks across all files:

### F1. tools.jsonl ↔ tool_graph.json node names
Every tool in tools.jsonl must have a node in tool_graph.json, and vice versa.
Severity: **error**

### F2. tools.jsonl ↔ tool_graph.json categories
Every node's category must match the tool's category in tools.jsonl.
Severity: **error**

### F3. tool_graph.json ↔ schema.sql table names
Every table name in node reads/writes must exist in schema.sql.
Severity: **error**

### F4. tool_graph.json edge validity
Every edge's `from` and `to` must reference valid node names.
Severity: **error**

### F5. query tools must not write
Nodes with category "query" must have empty `writes` arrays.
Severity: **error**

### F6. mutation tools must write
Nodes with category "mutation" should have non-empty `writes` arrays.
Severity: **warning**

### F7. ENVIRONMENT.md tool count
The number of tools in the Tools section must equal the lines in tools.jsonl.
Severity: **error**

### F8. backend.py tool registry
The `_TOOL_NAMES` set in backend.py must match tool names in tools.jsonl.
Severity: **error**

### F9. schema.sql ↔ world.db structure
Tables in schema.sql must match tables in world.db (same names and columns).
Severity: **error**

### F10. Test coverage
test_runtime.py should reference every tool name from tools.jsonl at least once.
Severity: **warning**

### F11. Conversational accessibility — ID discoverability
For every mutation tool that requires an internal ID parameter (e.g., `booking_id`, `order_id`, `user_id`), verify that at least one query tool exists that can return that ID given natural-language-friendly inputs (name, email, date, description, etc.). A mutation tool whose required IDs have no discovery path is unusable in a conversation.
Severity: **error**

### F12. Conversational accessibility — session field readability
Every column written to the sessions table by any tool must be readable by at least one query tool. Dead writes indicate state that no tool can surface to the user.
Severity: **warning**

## Part 3: Seed Data Volume

Open `world/world.db` and `spec/tool_graph.json`. Classify every table in the database and check row counts against minimum thresholds.

### Table classification

Derive the table type from `tool_graph.json` nodes:

1. **session** — the single-row `session` table. Exempt from volume checks.
2. **mutable** — any table that appears in at least one node's `writes` array (excluding the `session` table). These are entity or relationship tables modified by mutation tools.
3. **reference** — every other table (appears only in `reads` arrays or not referenced at all). These are read-only lookup/catalog tables.

### G1. Total row count floor

Count all rows across all tables (including `session`). The total must be **≥ 50**.

Severity: **error** if total < 50.

### G2. Healthy total row count

The total row count should be **≥ 80** for most environments. Below 80 indicates sparse seed data that limits task diversity.

Severity: **warning** if total is ≥ 50 but < 80.

### G3. User table minimum

Identify the primary user/account table (commonly named `users`, `accounts`, `operators`, `player_accounts`, or similar — the table that authentication tools read from). It must contain **≥ 5 rows** with distinct attribute profiles so tasks can target different people.

Severity: **error** if < 5 rows, or **error** if no identifiable user table exists in an environment that has authentication tools.

### G4. Mutable table minimum

Every mutable table (excluding `session`) must contain **≥ 8 rows**.

Severity: **error** for each table below 8. Report the table name and actual count.

### G5. Reference table minimum

Every reference table must contain **≥ 10 rows**.

Severity: **warning** for each table below 10. Report the table name and actual count.

### G6. Mutation tool scenario coverage

For each mutation tool (from `tool_graph.json` nodes with `category: "mutation"`), check that every table in its `writes` array has enough rows in distinct states to support **≥ 5 different task scenarios** without reusing the same entity. In practice, this means the writable target table must have ≥ 5 rows that the tool could plausibly operate on.

Severity: **warning** for each mutation tool whose writable tables have fewer than 5 eligible rows.

### G7. Column entropy — status and state fields

For every column whose name contains `status`, `state`, or `tier`, or that has a CHECK constraint with an IN clause listing 3+ values: count the distribution of values. If any single value accounts for **≥ 80%** of rows in a table with ≥ 8 rows, flag it.

Skip columns that are structurally low-entropy by design:
- Boolean flags: columns prefixed with `is_` or `supports_` (e.g., `is_bot`, `supports_audio`)
- Single-entity FK columns: if the environment is a single-workspace/team/deployment simulation, FK columns referencing that singleton entity are expected to be uniform
- CHECK constraints that allow only one value (e.g., `CHECK(run_type IN ('preview'))`)

Severity: **warning** for each flagged column. Report the table name, column name, dominant value, and its percentage.

### G8. Empty nullable columns

For every nullable column (no NOT NULL constraint) in a table with ≥ 8 rows: if the column is **100% NULL**, flag it. A column that is entirely NULL across all rows is likely a dead field that was never populated during synthesis. At least 2-3 rows should have non-NULL values for columns that participate in task-relevant logic (e.g., `cancelled_reason`, `delivered_at`, `error_message`).

Severity: **warning** for each fully-NULL column in a table with ≥ 8 rows.

## Output

```json
{
  "overall_pass": true,
  "tool_count": 19,
  "schema_errors": 0,
  "schema_warnings": 2,
  "consistency_errors": 0,
  "consistency_warnings": 1,
  "volume_errors": 0,
  "volume_warnings": 0,
  "issues": []
}
```

`overall_pass` is true only when `schema_errors`, `consistency_errors`, and `volume_errors` are all 0.

Issues use the same format as the individual checks: `{tool_name, severity, check, field, message}`. For volume checks (G-series), `tool_name` is `"_volume"` and `field` is the table name or `"_total"`.
