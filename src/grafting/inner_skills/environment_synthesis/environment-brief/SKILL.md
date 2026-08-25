---
name: environment-brief
description: Write ENVIRONMENT.md — the human-readable brief summarizing a packaged environment. Use this skill as the final step of environment synthesis after all other artifacts (tools.jsonl, tool_graph.json, schema.sql, world.db, backend.py, test_runtime.py) are complete. ENVIRONMENT.md serves as the primary reference for downstream task generators and human reviewers. It must be factually grounded in the actual artifacts, not invented.
---

# Environment Brief

You are a technical writer for packaged environments. Your job is to read all artifacts in an environment directory and produce `ENVIRONMENT.md` — a structured reference document that accurately summarizes the environment for downstream task generators and human reviewers.

## Sandbox Paths

- Read all artifacts from: `output/<env_name>/`
- Write output to: `output/<env_name>/spec/ENVIRONMENT.md`

## Input

Read these files from `output/<env_name>/`:

- `spec/tools.jsonl`: tool definitions (name, description, category, inputSchema)
- `spec/tool_graph.json`: tool dependency graph (nodes, edges)
- `world/schema.sql`: database schema
- `world/world.db`: seeded database with sample data
- `runtime/backend.py`: backend implementation (for session initialization details)
- `tests/test_runtime.py`: test suite (for verification guidance)

All of these must exist and be complete before you write ENVIRONMENT.md. If any artifact is missing, stop and report the missing artifact rather than writing a partial brief.

## Output

Write `output/<env_name>/spec/ENVIRONMENT.md`. The file must contain exactly 7 sections in the order specified below.

---

## Section 1: Title

```markdown
# <env_name>
```

The H1 title must match the environment directory name exactly. Do not embellish or rename it.

## Section 2: Overview

```markdown
## Overview
```

1-3 sentences describing what this environment simulates. State the domain, the types of operations supported, and any notable characteristics. Keep it factual and concise. Do not use marketing language.

Good: "A travel booking system supporting flight search, reservation, payment, and cancellation. Includes credit card management and invoice generation. All mutation operations require authentication."

Bad: "A powerful and comprehensive travel booking platform that efficiently handles all your travel needs."

## Section 3: Tools

```markdown
## Tools
```

Group tools by category. List mutation tools first, then query tools. Use a markdown table with two columns: Tool Name and Description.

```markdown
### Mutation Tools (N)

| Tool Name | Description |
|-----------|-------------|
| authenticate_travel | Authenticate a user and obtain an access token |
| book_flight | Book a flight between two airports |
...

### Query Tools (M)

| Tool Name | Description |
|-----------|-------------|
| search_flights | Search available flights between two cities |
...
```

Rules:
- The description column contains the first sentence of the tool's description from `tools.jsonl` (the core function statement). Truncate to one sentence.
- Every tool in `tools.jsonl` must appear in exactly one table. No extras, no missing.
- State the count in the subheading: "Mutation Tools (6)", "Query Tools (12)".

Add an **Authentication Requirements** subsection after the tables:

```markdown
### Authentication Requirements

Tools requiring authentication (access_token): book_flight, cancel_booking, ...
Tools without authentication requirement: search_flights, get_airport_list, ...
```

Determine this by checking which tools have an `access_token` parameter in their `inputSchema`.

## Section 4: Key Workflows

```markdown
## Key Workflows
```

Extract 4-6 representative workflows from `tool_graph.json` edges. Each workflow is a tool chain that represents a realistic user task.

Format each workflow as:

```markdown
### 1. <Workflow Title>

`tool_a` -> `tool_b` -> `tool_c` -> `tool_d`

One sentence explaining what this workflow accomplishes and when a user would follow it.
```

Rules:
- Every tool chain must trace a valid path through `tool_graph.json` edges. Do not invent connections that do not exist in the graph.
- Include a mix of simple workflows (3-4 tools) and complex workflows (5+ tools).
- Include at least one workflow that starts with authentication.
- Include at least one workflow that involves error recovery or alternative paths.
- Workflows should collectively cover most tools in the environment. Aim for every tool to appear in at least one workflow.

## Section 5: Database Schema

```markdown
## Database Schema
```

Present a table summarizing all tables in `schema.sql`:

```markdown
| Table | Rows | Type | Description |
|-------|------|------|-------------|
| users | 50 | reference | Registered user accounts with credentials |
| flight_routes | 200 | reference | Available flight connections between airports |
| bookings | 0 | mutable | Flight reservations created by users |
| credit_cards | 10 | mutable | Registered payment methods |
```

Column definitions:
- **Table**: table name from `schema.sql`
- **Rows**: actual row count from `world.db`. Query the database to get this. Do not guess.
- **Type**: `reference` for tables with pre-seeded data that tools only read, `mutable` for tables that tools create/modify/delete rows in. Determine this from `tool_graph.json` node writes — if any tool writes to a table, it is `mutable`.
- **Description**: one-line description of what the table stores

Add three subsections:

### Key Relationships

List foreign key relationships between tables. Format as: `table.column -> referenced_table.referenced_column`.

### Data Highlights

Note important value ranges, special formulas, data coverage, or constraints visible in the data. Examples:
- "Flight prices range from $150 to $2,400 based on route and class"
- "All user balances start at $10,000"
- "Airport codes cover 30 major international airports"

### Initial State

Describe what the database looks like at session start. Which tables have data, which are empty. This is critical for task generators to know what setup is needed.

## Section 6: Session Initialization

```markdown
## Session Initialization
```

Document the session state (via `world_setup` SQL) that `backend.py` accepts. Read the backend's `__init__` method or initialization logic to determine these.

```markdown
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| authenticated | bool | false | Whether the session starts pre-authenticated |
| current_user | dict | null | Pre-set user context (user_id, username, etc.) |
```

If the backend accepts no session state (via `world_setup` SQL), state: "This environment does not accept session initialization parameters."

## Section 7: Verification Guidance

```markdown
## Verification Guidance
```

Provide SQL queries that downstream evaluators can use to verify whether a task was completed successfully. Derive these from the test assertions in `test_runtime.py` and the tool descriptions.

Organize by verification type:

```markdown
### Booking Created

```sql
SELECT booking_id, user_id, flight_id, status
FROM bookings
WHERE booking_id = '{booking_id}';
-- Expected: one row with status = 'confirmed'
```

### Payment Deducted

```sql
SELECT balance FROM credit_cards WHERE card_id = '{card_id}';
-- Expected: balance decreased by flight cost
```
```

Rules:
- Include one subsection per major mutation type (create, update, delete, transfer, etc.).
- Each subsection contains a SQL template with placeholder values in `{curly_braces}`.
- Each SQL block includes a comment stating the expected result.
- Queries should be simple SELECTs that an evaluator can run against the session database after task execution.

## Quality Criteria

Before finalizing ENVIRONMENT.md, verify:

1. **Tool completeness**: Every tool in `tools.jsonl` appears in the Tools section. Count matches.
2. **Workflow validity**: Every workflow traces a valid path through `tool_graph.json` edges.
3. **Row count accuracy**: Row counts in Database Schema match actual `world.db` counts. Query the database.
4. **Sample data authenticity**: Any data examples are actual rows from `world.db`, not invented.
5. **Session parameters accuracy**: Init context parameters match what `backend.py` actually accepts.
6. **No hallucination**: Every fact in the document is sourced from an artifact. Do not invent features, constraints, or behaviors.

## Important

- Read all artifacts before writing. Do not start writing after reading only `tools.jsonl`.
- Query `world.db` directly for row counts and sample data. Do not estimate or guess.
- The brief must be self-contained. A reader with no other context should understand the environment's capabilities, data model, and verification approach from this document alone.
- Use consistent formatting throughout. Match the markdown patterns shown in this guide exactly.
- Do not add sections beyond the 7 specified. Do not omit any of the 7 sections.
