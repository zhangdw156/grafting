---
name: contract-alignment
description: Fix mismatches between tools.jsonl, backend.py, tool_graph.json, and schema.sql. Ensures all four artifacts are consistent in tool names, parameter names, categories, and table references.
---

# Contract Alignment

Fix cross-artifact consistency issues where tool definitions, backend implementation, tool graph, and database schema disagree.

## Inputs

- `output/diagnosis.json` — structured diagnosis from assess-diagnosis
- `env/spec/tools.jsonl` — tool definitions (authoritative for names and params)
- `env/spec/tool_graph.json` — tool dependency graph
- `env/runtime/backend.py` — backend implementation
- `env/world/schema.sql` — database schema

## Authority Hierarchy

When artifacts disagree, fix according to this priority (highest first):

1. **tools.jsonl** — authoritative for tool names, parameter names, categories
2. **schema.sql** — authoritative for table names, column names, constraints
3. **backend.py** — must conform to tools.jsonl (params) and schema.sql (SQL)
4. **tool_graph.json** — must conform to tools.jsonl (names, categories) and schema.sql (table refs)

## Alignment Checks

### A1. Tool name consistency
Every tool name must appear in all four artifacts:
- tools.jsonl `name` field
- backend.py `_TOOL_NAMES` frozenset
- backend.py `_tool_<name>` method
- tool_graph.json node `name` field

### A2. Parameter name consistency
tools.jsonl `inputSchema.properties` keys must exactly match backend.py `_tool_<name>` method parameter names (excluding `self`).

### A3. Category consistency
tools.jsonl `category` must match tool_graph.json node `category` for every tool.

### A4. Table name consistency
tool_graph.json node `reads` and `writes` arrays must reference tables that exist in schema.sql.

### A5. Required/optional consistency
tools.jsonl `required` array members must be non-default parameters in backend.py. Parameters NOT in `required` must have defaults in backend.py.

## Process

1. Read `output/diagnosis.json` for identified mismatches
2. Load all four artifacts into a unified registry:
   ```
   For each tool:
     tools.jsonl: {name, params, required, category}
     backend.py:  {_TOOL_NAMES entry, method name, method params}
     tool_graph:  {node name, category, reads, writes}
   ```
3. Run checks A1-A5, collecting all discrepancies
4. Apply fixes per authority hierarchy
5. Re-verify after all changes

## Output

- Modified files in place under `env/`
- Write `output/alignment_fixes.json`:

```json
{
  "fixes": [
    {
      "check": "A2",
      "tool": "search_contacts",
      "detail": "Renamed backend param 'query_text' to 'search_query' to match tools.jsonl",
      "file": "env/runtime/backend.py"
    }
  ]
}
```
