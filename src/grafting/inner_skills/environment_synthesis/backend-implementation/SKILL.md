---
name: backend-implementation
description: Guide for implementing runtime/backend.py for Grafting packaged environments. Covers the EnvironmentBackend class structure, dispatch pattern, helper methods (_ok, _error, _session, _check_access, _next_id), tool implementation pattern, and critical rules (all state in SQLite, no exceptions from call(), no randomness, no external calls, stdlib-only imports).
---

# Backend Implementation

This skill provides the complete guide for implementing `runtime/backend.py`. The backend is the executable core of a packaged environment. It connects to a session-local SQLite database and implements every tool defined in `tools.jsonl`.

## Sandbox Paths

- Read tool definitions from: `output/<env_name>/spec/tools.jsonl`
- Read schema from: `output/<env_name>/world/schema.sql`
- Write output to: `output/<env_name>/runtime/backend.py`

The reference implementation is the travel_booking backend, which implements 19 tools against a SQLite database. The patterns described here are extracted from that implementation and are the canonical way to build backends.

## Class Structure

```python
"""SQLite-backed <domain> backend.

Implements N tools against a session-local SQLite copy of world.db.
All state reads/writes go through SQLite. ``call()`` never raises;
errors are returned as ``{"success": false, "error": {"code": "...", "message": "..."}}`` dicts.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class EnvironmentBackend:
    """<Domain description> backed by a session-local SQLite database."""

    def __init__(self, *, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    _TOOL_NAMES = frozenset({
        "tool_one",
        "tool_two",
        # ... all tool names from tools.jsonl ...
    })

    def call(self, tool_name: str, arguments: dict) -> dict:
        """Dispatch *tool_name* to its handler. Never raises."""
        try:
            if tool_name not in self._TOOL_NAMES:
                return self._error("UNKNOWN_TOOL", f"Unknown tool: {tool_name}")
            method = getattr(self, f"_tool_{tool_name}")
            return method(**arguments)
        except Exception as exc:  # noqa: BLE001
            return self._error("INTERNAL_ERROR", str(exc))

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def visible_state(self) -> dict:
        """Return a concise user-visible summary of the session."""
        # Query DB for key indicators
        # NOT the full DB -- a meaningful projection
        # Example: logged_in status, counts of records, balances
        ...

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any) -> dict:
        """Build a success response."""
        return {"success": True, "data": data}

    @staticmethod
    def _error(code: str, message: str) -> dict:
        """Build an error response."""
        return {"success": False, "error": {"code": code, "message": message}}

    def _session(self) -> dict:
        """Return the session row as a plain dict."""
        row = self._conn.execute(
            "SELECT * FROM session WHERE session_id = 1"
        ).fetchone()
        return dict(row)

    def _check_access(self, access_token: str) -> dict | None:
        """Return an error dict if *access_token* is invalid/expired, else ``None``."""
        ...

    def _next_id(self, table: str, column: str, prefix: str, width: int) -> str:
        """Generate the next deterministic ID for *table*.*column*."""
        ...

    # ==================================================================
    # Tool implementations
    # ==================================================================

    def _tool_example(self, access_token: str, param: str) -> dict:
        ...
```

## Helpers in Detail

### `_ok(data)` and `_error(code, message)`

These are static methods that build the unified return format. Every tool handler must return one of these two.

```python
@staticmethod
def _ok(data: Any) -> dict:
    return {"success": True, "data": data}

@staticmethod
def _error(code: str, message: str) -> dict:
    return {"success": False, "error": {"code": code, "message": message}}
```

Never build response dicts manually. Always use `self._ok(...)` or `self._error(...)`.

### `_session()`

Reads the single session row and returns it as a plain dict. Used by `_check_access` and any tool that needs session state.

```python
def _session(self) -> dict:
    row = self._conn.execute(
        "SELECT * FROM session WHERE session_id = 1"
    ).fetchone()
    return dict(row)
```

### `_check_access(access_token)`

Validates an access token against the session table. Returns an error dict if the token is invalid, or `None` if it is valid. Three error codes:

1. `TOKEN_NOT_INITIALIZED`: No token exists in the session table (user has not authenticated).
2. `TOKEN_EXPIRED`: The token exists but its expiry timestamp is in the past.
3. `INVALID_ACCESS_TOKEN`: A token exists and is not expired, but the provided value does not match.

```python
def _check_access(self, access_token: str) -> dict | None:
    s = self._session()
    if s["token_expires_at"] is None:
        return self._error(
            "TOKEN_NOT_INITIALIZED",
            "No authentication token found. Call <auth_tool> first.",
        )
    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if s["token_expires_at"] <= now:
        return self._error(
            "TOKEN_EXPIRED",
            "Authentication token has expired. Call <auth_tool> to obtain a new token.",
        )
    if access_token != s["access_token"]:
        return self._error(
            "INVALID_ACCESS_TOKEN",
            "The provided access token is invalid.",
        )
    return None
```

Token expiry uses ISO 8601 string comparison. This is deterministic and does not depend on call counting.

### `_next_id(table, column, prefix, width)`

Generates the next deterministic ID by querying the current maximum numeric suffix and incrementing it.

```python
def _next_id(self, table: str, column: str, prefix: str, width: int) -> str:
    row = self._conn.execute(
        f"SELECT COALESCE(MAX(CAST(SUBSTR({column}, {len(prefix) + 1}) AS INTEGER)), 0) + 1 AS nxt FROM {table}"
    ).fetchone()
    return f"{prefix}{row['nxt']:0{width}d}"
```

Example usage:
```python
booking_id = self._next_id("bookings", "booking_id", "BK", 7)
# If max existing is BK0000003, returns BK0000004
# If table is empty, returns BK0000001
```

This is deterministic: given the same database state, it always produces the same ID. No randomness, no UUIDs.

### Pure computation helpers

For business logic that does not touch the database, use `@staticmethod` methods.

```python
@staticmethod
def _compute_cost(base_cost: int, travel_class: str, travel_date: str) -> float:
    class_factors = {"economy": 1, "business": 2, "first": 5}
    factor = class_factors[travel_class]
    digit_sum = sum(int(ch) for ch in travel_date if ch.isdigit())
    date_multiplier = 2 if digit_sum % 2 == 0 else 1
    return float(base_cost * factor * date_multiplier)
```

These must be pure functions: no database access, no side effects, no randomness.

## Session State Initialization

Session state (authentication, user identity, domain-specific settings) is initialized via `world_setup` SQL statements in `task.json` BEFORE the backend is instantiated. The backend does not receive or process any initialization parameters -- it simply connects to the database and reads whatever state the `world_setup` SQL has already established.

This means:
- The `__init__` method only connects to the database. It does not apply any context.
- The session table in `world.db` starts with NULL/default values.
- The `world_setup` SQL in `task.json` runs UPDATE statements against the session table to set authentication tokens, user identity, domain-specific state, etc.
- By the time the backend is instantiated, the database already reflects the desired session state.

## Tool Implementation Pattern

Every tool handler follows the same structure.

### Template

```python
def _tool_<name>(self, access_token: str, param1: str, param2: int, ...) -> dict:
    # 1. Check authentication (if required)
    err = self._check_access(access_token)
    if err:
        return err

    # 2. Validate inputs (return _error for each invalid input)
    if param1 not in valid_values:
        return self._error("INVALID_PARAM1", f"Invalid value: {param1}")

    # 3. Read DB state
    row = self._conn.execute("SELECT ... WHERE ...").fetchone()
    if row is None:
        return self._error("NOT_FOUND", f"No record found with id '{param1}'.")

    # 4. Check business rules
    if row["balance"] < required_amount:
        return self._error("INSUFFICIENT_FUNDS", "...")

    # 5. Execute mutation (UPDATE/INSERT/DELETE)
    self._conn.execute("INSERT INTO ... VALUES ...", (...,))
    self._conn.commit()

    # 6. Return success
    return self._ok({"id": new_id, "status": "created"})
```

### Steps explained

1. **Authentication check**: If the tool requires auth, call `_check_access` first. Return immediately if it fails.

2. **Input validation**: Validate every input parameter. Check enum values, format constraints, range bounds. Return a specific error code for each failure. Do not let invalid values reach the database.

3. **Read DB state**: Query the database for records needed by the tool. Check for existence (return `NOT_FOUND` errors for missing records).

4. **Business rule checks**: Apply domain-specific rules. Check balances, budgets, permissions, date constraints, uniqueness. Each rule failure returns a specific error code.

5. **Execute mutation**: For mutation tools, execute the SQL statement(s) and commit. Always commit after all writes in a single tool call.

6. **Return success**: Use `self._ok(...)` with a meaningful payload. Include IDs, amounts, statuses -- whatever the caller needs for subsequent steps.

### Query tool variant

Query tools are simpler: they skip the mutation step and often skip authentication.

```python
def _tool_list_all_airports(self) -> dict:
    rows = self._conn.execute(
        "SELECT iata_code, city_name FROM airports ORDER BY iata_code"
    ).fetchall()
    return self._ok([
        {"iata_code": r["iata_code"], "city_name": r["city_name"]}
        for r in rows
    ])
```

## Dispatch Mechanism

The `call()` method dispatches to tool handlers by name.

```python
_TOOL_NAMES = frozenset({
    "authenticate_travel",
    "book_flight",
    "cancel_booking",
    # ... every tool name from tools.jsonl ...
})

def call(self, tool_name: str, arguments: dict) -> dict:
    try:
        if tool_name not in self._TOOL_NAMES:
            return self._error("UNKNOWN_TOOL", f"Unknown tool: {tool_name}")
        method = getattr(self, f"_tool_{tool_name}")
        return method(**arguments)
    except Exception as exc:  # noqa: BLE001
        return self._error("INTERNAL_ERROR", str(exc))
```

### Rules

- `_TOOL_NAMES` is a class-level `frozenset` containing every tool name. It must match `tools.jsonl` exactly.
- Tool handlers are named `_tool_<name>` where `<name>` matches the tool name in `tools.jsonl`.
- The `try/except` wraps the entire dispatch including argument unpacking. If the LLM passes wrong argument names or types, the `TypeError` from `**arguments` unpacking is caught and returned as `INTERNAL_ERROR`.
- The except clause uses `Exception` (not a narrower type) to guarantee `call()` never raises.

## visible_state() Implementation

`visible_state()` returns a concise summary of the session, not a full database dump.

```python
def visible_state(self) -> dict:
    row = self._conn.execute(
        """
        SELECT access_token, token_expires_at,
               user_first_name, user_last_name, budget_limit
        FROM session WHERE session_id = 1
        """
    ).fetchone()
    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    logged_in = (
        row["token_expires_at"] is not None and row["token_expires_at"] > now
    )
    user = (
        f"{row['user_first_name']} {row['user_last_name']}"
        if row["user_first_name"]
        else None
    )
    num_cards = self._conn.execute(
        "SELECT COUNT(*) AS c FROM credit_cards"
    ).fetchone()["c"]
    num_bookings = self._conn.execute(
        "SELECT COUNT(*) AS c FROM bookings"
    ).fetchone()["c"]
    return {
        "logged_in": logged_in,
        "user": user,
        "num_credit_cards": num_cards,
        "num_bookings": num_bookings,
        "budget_limit": row["budget_limit"],
    }
```

### What to include

- Authentication status (logged in / not logged in)
- Current user identity
- Counts of key mutable records (bookings, cards, orders)
- Session-level settings (budget limit, preferences)

### What NOT to include

- Full database contents
- Raw SQL rows
- Sensitive data (tokens, card numbers, passwords)
- Reference table data (airports, routes -- these never change)

## No-Auth Environments

Not every environment has authentication. For domains like file systems, vehicle control, or IoT devices, there is no user login — the session is always "active" and `world_setup` SQL configures the initial physical/logical state instead.

### world_setup SQL for no-auth domains

Session state for no-auth environments is set via `world_setup` SQL in `task.json` before the backend is instantiated. For example:

```sql
-- File system: set starting directory
UPDATE session SET current_directory_id = 5 WHERE session_id = 1;

-- Vehicle: set initial physical state
UPDATE vehicle_state SET fuel_level = 8.5, engine_state = 'stopped' WHERE vehicle_id = 1;
```

Key difference from auth environments:
- No `_check_access` helper needed — tools do not take an `access_token` parameter.
- `world_setup` SQL sets domain-specific state (fuel level, cwd, device mode) instead of auth tokens.
- The session table still exists but holds domain state, not auth state.

## Recursive Operations

For environments with tree-structured data (file systems, org charts, nested categories), tools like delete, copy, and size computation need to traverse the tree recursively.

### Pattern: recursive helper

```python
def _delete_recursive(self, inode_id: int) -> None:
    children = self._conn.execute(
        "SELECT inode_id, type FROM inodes WHERE parent_id = ?",
        (inode_id,),
    ).fetchall()
    for child in children:
        if child["type"] == "dir":
            self._delete_recursive(child["inode_id"])
        self._conn.execute(
            "DELETE FROM inodes WHERE inode_id = ?", (child["inode_id"],)
        )

def _copy_recursive(self, src_id: int, dest_parent_id: int, new_name: str) -> int:
    src = self._conn.execute(
        "SELECT * FROM inodes WHERE inode_id = ?", (src_id,)
    ).fetchone()
    self._conn.execute(
        "INSERT INTO inodes (parent_id, name, type, content, size, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dest_parent_id, new_name, src["type"], src["content"],
         src["size"], datetime.now(tz=UTC).isoformat()),
    )
    new_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if src["type"] == "dir":
        for child in self._conn.execute(
            "SELECT * FROM inodes WHERE parent_id = ?", (src_id,)
        ).fetchall():
            self._copy_recursive(child["inode_id"], new_id, child["name"])
    return new_id
```

Key rules:
- Use Python recursion over SQL, since SQLite recursive CTEs are harder to debug.
- Always delete children before parent (foreign key safety).
- Commit only once at the tool-method level, not inside the recursive helper.

## Critical Rules

These rules are non-negotiable. Violating any of them produces an invalid backend.

### Rule 1: ALL state in SQLite

No instance variables holding mutable state. The `self._conn` connection is the only instance attribute. All data reads come from `SELECT`, all data writes go through `INSERT`/`UPDATE`/`DELETE` + `commit()`.

Why: session isolation depends on the database copy. If state lives in Python memory, it is invisible to the session mechanism and will not be restored correctly.

The only acceptable instance attribute beyond `self._conn` is a cached reference that is derived from immutable data (e.g., a config value read once from the database that never changes).

### Rule 2: call() NEVER raises

The `call()` method must catch all exceptions and return them as `INTERNAL_ERROR` responses. The trajectory runtime assumes `call()` always returns a dict. An unhandled exception will crash the simulation.

### Rule 3: No randomness

Do not import `random`. Do not use `uuid.uuid4()`. Do not use `secrets`. All outputs must be deterministic given the same database state and inputs. Use `_next_id` for ID generation.

### Rule 4: No external calls

No HTTP requests. No file reads outside the database. No subprocess calls. No socket connections. The backend is a pure database state machine.

### Rule 5: Stdlib-only imports

The only allowed imports are from the Python standard library:
- `sqlite3` (database access)
- `datetime` (time handling: `datetime`, `timedelta`, `UTC`)
- `pathlib` (the `Path` type for `db_path`)
- `typing` (type annotations: `Any`)

No third-party packages. No `requests`, no `json` (unless parsing tool arguments), no `numpy`, no `pandas`.

### Rule 6: Token expiry by time, not call count

Token validity is determined by comparing `token_expires_at` (an ISO 8601 string in the session table) against the current time. Never count the number of `call()` invocations to determine token expiry.

### Rule 7: Use datetime.now(tz=UTC), not datetime.utcnow()

`datetime.utcnow()` is deprecated in Python 3.12+. Always use `datetime.now(tz=UTC)` instead. The standard import line is:
```python
from datetime import UTC, datetime, timedelta
```

### Rule 8: Commit after mutations

Every tool that modifies the database must call `self._conn.commit()` before returning. Uncommitted changes are invisible to subsequent tool calls within the same session if the connection is shared.

### Rule 9: Return masked sensitive data

When returning credit card information, return `last_four_digits` (the last 4 characters of `card_number`), not the full card number. Never return CVV/CVC values in any response.
