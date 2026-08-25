---
name: environment-contract
description: Canonical reference for the Grafting packaged environment format. Defines the required package layout, EnvironmentBackendProtocol, ToolGraph data model, unified return format, tools.jsonl schema, ENVIRONMENT.md sections, schema.sql conventions, and planner-runtime handoff. All other environment synthesis skills depend on this contract.
---

# Environment Contract

This is the foundational reference that defines the canonical packaged environment format. Every environment produced by the environment synthesis must conform to this contract. All other environment synthesis skills (tool-surface-design, schema-design, backend-implementation) depend on and reference this document.

## Package Layout

A valid packaged environment contains exactly 6 required files plus an optional tests directory:

```text
<env_name>/
  spec/
    tools.jsonl          # MCP tool schemas (one JSON object per line)
    tool_graph.json      # Tool dependency graph (nodes + edges)
    ENVIRONMENT.md       # Human-readable environment brief
  runtime/
    backend.py           # Executable environment backend
  world/
    world.db             # SQLite database with schema + seed data
    schema.sql           # SQL DDL that creates the empty schema
  tests/
    test_runtime.py      # Runtime-oriented environment tests (optional but recommended)
```

The directory name is the environment ID. No configuration files are needed -- convention over configuration.

All 6 required files must exist for the environment to pass validation. The `tests/` directory is strongly recommended but not enforced by the contract checker.

## Design Principles

### Program-authority

All environments are program-authority: deterministic, code-driven. The backend decides every outcome. There is no LLM in the loop at runtime. Given the same database state and the same tool call, the result is always identical.

### SQLite-backed state with session isolation

All mutable state lives in a SQLite database (`world.db`). Session isolation is achieved by copying `world.db` to a session-local path before instantiation. Each session operates on its own copy. No shared mutable state exists between sessions.

### Convention over configuration

There are no config files. The directory name is the environment ID. The package layout is fixed. The backend class is always named `EnvironmentBackend`. The session table always has `session_id = 1`.

### Decoupled synthesis and task generation

Environment synthesis produces the packaged environment. Task generation (trajectory planning) is a separate downstream process. The environment package contains no tasks, no scenarios, no transcripts. It contains the tools, the world state, and the runtime -- nothing more.

### Tool count lower bound

A packaged environment must contain at least 12 tools. Fewer than 12 means the environment is too simple to generate interesting multi-step tasks. There is no upper limit — environments like GitHub API simulations may have 60+ tools.

## EnvironmentBackendProtocol

Every `backend.py` must export a class named `EnvironmentBackend` that implements this protocol:

```python
class EnvironmentBackend:
    def __init__(self, *, db_path: Path) -> None: ...
    def call(self, tool_name: str, arguments: dict) -> dict: ...
    def visible_state(self) -> dict: ...
```

### `__init__(self, *, db_path: Path, world_setup: dict) -> None`

- `db_path` is the path to the session-local SQLite database (a copy of `world.db`).
- `world_setup` SQL is a dictionary of session initialization parameters (e.g., `authenticated`, `current_user`, `budget_limit`). The backend must use `get()` with defaults for all keys -- never crash on missing keys.
- Connect to SQLite with WAL mode, foreign keys ON, and `sqlite3.Row` row factory.
- Apply `world_setup` SQL to the session table.

### `call(self, tool_name: str, arguments: dict) -> dict`

- Dispatch to the appropriate tool handler.
- Must never raise exceptions. All errors are returned as structured error dicts.
- Unknown tool names return `{"success": false, "error": {"code": "UNKNOWN_TOOL", "message": "..."}}`.
- Any unhandled exception is caught and returned as `{"success": false, "error": {"code": "INTERNAL_ERROR", "message": "..."}}`.

### `visible_state(self) -> dict`

- Return a user-visible summary of the current session state.
- This is NOT the full database -- it is a meaningful projection (e.g., logged-in status, number of bookings, balance).
- Used by the trajectory planner to understand the current state without dumping raw SQL.

### Critical constraints

- All state lives in the database. No in-memory-only mutable state on instance variables.
- Deterministic transitions: no `random` module, no `uuid4`, no external calls, no network requests.
- No dependency on synthesis artifacts at runtime. The backend reads only from its SQLite database.
- No imports beyond the standard library (`sqlite3`, `datetime`, `pathlib`, `typing`).

## ToolGraph Data Model

The `tool_graph.json` file describes the dependency structure between tools. It has two sections: `nodes` and `edges`. There is no `state_entities` section -- entity information is redundant with `schema.sql` and the `reads`/`writes` fields on nodes.

### Nodes

Each node represents one tool:

```json
{
  "name": "book_flight",
  "category": "mutation",
  "reads": ["flight_routes", "credit_cards", "session"],
  "writes": ["bookings", "credit_cards"]
}
```

Fields:
- `name` (string): tool name, must match a name in `tools.jsonl`.
- `category` (string): `"query"` or `"mutation"`.
- `reads` (array of strings): table names this tool reads from.
- `writes` (array of strings): table names this tool writes to. Empty for query tools.

### Edges

Each edge represents a dependency between two tools:

```json
{
  "from": "authenticate_travel",
  "to": "book_flight",
  "type": "state_precondition",
  "via": "access_token"
}
```

Fields:
- `from` (string): source tool name.
- `to` (string): target tool name.
- `type` (string): either `"data_dependency"` or `"state_precondition"`.
  - `data_dependency`: the output of `from` is used as input to `to` (e.g., a booking_id returned by `book_flight` is needed by `retrieve_invoice`).
  - `state_precondition`: `from` must be called before `to` to establish required state (e.g., authentication must happen before booking).
- `via` (string): the field or concept that connects the two tools (e.g., `"access_token"`, `"booking_id"`, `"card_id"`).

## Unified Return Format

Every tool must return one of exactly two shapes. No exceptions.

### Success

```json
{"success": true, "data": <payload>}
```

`<payload>` can be any JSON value: object, array, string, number, boolean, or null. It must contain the meaningful result of the operation.

### Error

```json
{"success": false, "error": {"code": "ERROR_CODE", "message": "Human readable message"}}
```

- `code` is SCREAMING_SNAKE_CASE (e.g., `CARD_NOT_FOUND`, `INSUFFICIENT_FUNDS`, `TOKEN_EXPIRED`).
- `message` is a human-readable, actionable description of the error. It should tell the caller what went wrong and, when possible, what to do about it.

### Why this matters

A unified return format means the trajectory planner and evaluator can parse tool results without per-tool logic. It also means the LLM agent can reliably detect success vs. failure across all tools in the environment.

## tools.jsonl Format

Each line in `tools.jsonl` is a JSON object with these fields:

```json
{
  "name": "book_flight",
  "description": "Book a flight between two airports, charging the specified credit card. ...",
  "category": "mutation",
  "inputSchema": {
    "type": "object",
    "properties": {
      "access_token": {"type": "string", "description": "..."},
      "card_id": {"type": "string", "description": "..."},
      "travel_class": {
        "type": "string",
        "description": "...",
        "enum": ["economy", "business", "first"]
      }
    },
    "required": ["access_token", "card_id", "travel_class"]
  }
}
```

### Field definitions

- `name` (string): the tool identifier. Must match the handler method name in `backend.py` (minus the `_tool_` prefix).
- `description` (string): tool-level description. See the tool-surface-design skill for quality requirements.
- `category` (string): exactly `"query"` or `"mutation"`.
  - `query`: reads state without modifying it. No side effects.
  - `mutation`: modifies state. Includes authentication, registration, creation, update, and deletion operations.
- `inputSchema` (object): JSON Schema for the tool's parameters.
  - Must use `"enum"` arrays for parameters with a fixed set of valid values.
  - The `"required"` array must list all mandatory parameters.
  - Each property must have a `"description"` field.

## ENVIRONMENT.md Required Sections

The `ENVIRONMENT.md` file is a human-readable brief for the environment. It must contain exactly these 7 sections:

### 1. Overview

One paragraph describing the environment's domain, purpose, and scope. What real-world system does it model? What can an agent accomplish in this environment?

### 2. Tools

A table or list of all tools with name, category, and one-line description. Must match `tools.jsonl` exactly.

### 3. Key Workflows

Describe 3-5 common multi-step workflows that an agent would perform. Each workflow lists the tools involved in sequence and explains the business logic. Example: "Book a flight: authenticate -> get_flight_cost -> register_credit_card -> book_flight".

### 4. Database Schema

Describe all tables, their purposes, and whether they are reference (read-only) or mutable. Include column names and types for key tables. Must match `schema.sql`.

### 5. Sample Data

Describe the pre-seeded data in `world.db`. How many airports, routes, credit cards, existing bookings, etc.? What edge cases are included?

### 6. Session Initialization

Describe the session state (via `world_setup` SQL) the backend accepts and how they affect the session table. Include examples of authenticated vs. unauthenticated initialization.

### 7. Verification Guidance

Describe how to verify the environment is working correctly. What tool calls should succeed? What error conditions should be testable? Include 2-3 concrete verification scenarios.

## schema.sql Conventions

### Naming

- Tables: `snake_case` (e.g., `flight_routes`, `credit_cards`).
- Columns: `snake_case` (e.g., `card_number`, `travel_date`).
- Primary keys: `<entity>_id` (e.g., `booking_id`, `card_id`, `insurance_id`).

### Foreign keys

Declare foreign keys explicitly:

```sql
FOREIGN KEY (card_id) REFERENCES credit_cards(card_id)
```

### Reference tables vs. mutable tables

- Reference tables are read-only. Tools never INSERT, UPDATE, or DELETE rows in them. They contain static data like airport codes, currency exchange rates, product catalogs.
- Mutable tables are modified by mutation tools. They contain bookings, transactions, registered cards, session state.

The distinction is determined by whether any tool writes to the table, not by a flag in the schema.

### CHECK constraints

Use CHECK constraints for enum-like columns:

```sql
travel_class TEXT NOT NULL CHECK(travel_class IN ('economy', 'business', 'first'))
```

### Executability

The file must be directly executable: `sqlite3 new.db < schema.sql` must create a valid empty schema with all tables, constraints, and indexes. It contains no INSERT statements -- those live only in `world.db`.

## What Must NOT Be Packaged

The following must not be included in a packaged environment:

- **Raw source skill files** (except `raw_skill/SKILL.md` which is kept for provenance but not used at runtime)
- **Synthesis artifacts**: intermediate outputs, LLM transcripts, generation logs
- **Configuration files**: no YAML, no TOML, no JSON configs
- **Scenarios or tasks**: no `scenarios/` directory, no `default.json`, no task blueprints
- **Pre-generated tasks or trajectories**: no `tasks.json`, no `trajectories/`

The packaged environment is a clean, self-contained runtime artifact. Everything the backend needs is in `world.db` and `schema.sql`.

## Planner-Runtime Handoff

### What the planner sees

The trajectory planner receives:
- `spec/tools.jsonl`: the complete tool surface
- `spec/tool_graph.json`: tool dependencies
- `spec/ENVIRONMENT.md`: human-readable brief
- `world/schema.sql`: database schema (for understanding data model)

The planner does NOT directly access `world.db` or `backend.py`.

### What the planner produces

The planner generates per-task artifacts:
- A task description with user intent
- An `world_setup` SQL dict for session initialization
- Expected tool call sequences

### Runtime flow

1. **Copy DB**: `world.db` is copied to a session-local path (`session.db`).
2. **Instantiate**: `EnvironmentBackend(db_path=session_db)` is called.
3. **Simulate**: The LLM agent makes tool calls via `backend.call(tool_name, arguments)`.
4. **Diff**: The evaluator compares initial vs. final `visible_state()` and tool call history against expectations.

Each session is fully isolated. Multiple trajectories can run in parallel without interference.
