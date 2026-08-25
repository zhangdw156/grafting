---
name: seed-data-enrichment
description: Add rows and diversity to world.db for environments that are too simple (all accepted) or have data issues (sparse tables, low entropy). Follows the data richness requirements from the environment contract.
---

# Seed Data Enrichment

Add data volume and diversity to world.db to support richer task generation.

## Inputs

- `env/world/world.db` — seed database to enrich
- `env/world/schema.sql` — database schema
- `env/spec/tools.jsonl` — tool definitions
- `env/spec/tool_graph.json` — tool graph (for mutable vs reference classification)
- `output/diagnosis.json` — optional, from assess-diagnosis

## When to Use

- **Too simple environments** (5/5 accepted): increase complexity so tasks require more steps and edge-case handling
- **Data-sparse environments**: tables below minimum row counts, columns with low entropy
- **FK diversity issues**: all rows referencing the same parent entity

## Enrichment Rules

### Volume minimums (from contract)
- Entity tables (users, projects, orders): ≥ 8 rows
- Relationship tables (assignments, memberships): ≥ 10 rows
- Reference/lookup tables (products, categories): ≥ 15 rows
- User/account table: ≥ 5 distinct profiles
- Total: ≥ 80 rows

### Entropy requirements
- Status/state columns: no single value > 60% of rows
- Nullable columns: 15-30% non-NULL for task-relevant fields
- FK columns: reference at least 3 different parent rows
- Boolean feature flags (`is_*`, `supports_*`): exempt from entropy rules

### Data quality
- All new IDs follow the environment's format (prefix + zero-padded number)
- No ID collisions with existing data
- All FK references point to existing rows
- All CHECK constraints satisfied
- Data must be realistic and English-only
- Do not modify schema.sql — only add data to world.db

## Process

1. Open world.db and read schema.sql
2. Classify tables using tool_graph.json: `writes` → mutable, else reference
3. For each table:
   a. Count rows — compare against minimums
   b. For columns with CHECK(... IN ...) constraints, check value distribution
   c. For nullable columns, count NULL vs non-NULL
   d. For FK columns, count distinct referenced parents
4. Generate INSERT statements for new data
5. Execute against world.db
6. If "too simple" environment: also add edge-case data:
   - Records with near-limit values (near-zero balance, near-expiry dates)
   - Records in unusual statuses (cancelled, expired, suspended)
   - Records that would trigger error paths in mutation tools
7. Verify FK integrity and CHECK constraints after all insertions

## Output

- Modified `env/world/world.db` in place
- Write `output/enrichment_report.json`:

```json
{
  "tables_modified": ["orders", "users", "products"],
  "rows_added": 42,
  "total_rows_after": 156,
  "entropy_fixes": [
    {"table": "orders", "column": "status", "before": "90% active", "after": "45% active, 25% completed, 15% cancelled, 15% pending"}
  ]
}
```
