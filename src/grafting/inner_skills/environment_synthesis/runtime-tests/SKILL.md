---
name: runtime-tests
description: Write tests/test_runtime.py for a packaged environment. Use this skill after backend.py, schema.sql, world.db, tools.jsonl, and tool_graph.json are complete. Produces a comprehensive pytest test suite that validates the backend implementation against the tool contract. Tests use dynamic loading (no grafting package dependency) and cover smoke tests, tool coverage, workflow tests, error cases, protocol compliance, and DB isolation.
---

# Runtime Tests

You are a test engineer for packaged environments. Your job is to read all artifacts in an environment directory and produce `tests/test_runtime.py` — a comprehensive pytest test suite that validates the backend implementation.

## Sandbox Paths

- Read all artifacts from: `output/<env_name>/`
- Write output to: `output/<env_name>/tests/test_runtime.py`

## Input

Read these files from `output/<env_name>/`:

- `spec/tools.jsonl`: tool definitions (name, description, category, inputSchema)
- `spec/tool_graph.json`: tool dependency graph (nodes, edges)
- `world/schema.sql`: database schema
- `runtime/backend.py`: the backend implementation to test
- `world/world.db`: seeded database

## Output

Write `output/<env_name>/tests/test_runtime.py`.

## Test Infrastructure

Tests must use dynamic loading so they do not depend on the `grafting` package. Use this exact boilerplate at the top of the file:

```python
"""Runtime tests for <env_name> environment."""

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ENV_ROOT = Path(__file__).resolve().parent.parent


def _load_backend(db_path):
    """Dynamically load the backend module and instantiate EnvironmentBackend."""
    spec = importlib.util.spec_from_file_location(
        "backend", ENV_ROOT / "runtime" / "backend.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EnvironmentBackend(db_path=str(db_path))
```

### Standard Fixtures

Every test file must define these two fixtures:

```python
@pytest.fixture
def backend(tmp_path):
    """Backend with default (unauthenticated) session."""
    db = tmp_path / "session.db"
    shutil.copy2(ENV_ROOT / "world" / "world.db", db)
    return _load_backend(db)


@pytest.fixture
def auth_backend(tmp_path):
    """Backend with pre-authenticated session.

    Session state is set via SQL (mirroring world_setup in task.json)
    before the backend is instantiated.
    """
    db = tmp_path / "session.db"
    shutil.copy2(ENV_ROOT / "world" / "world.db", db)
    # Apply world_setup SQL to set session state before instantiation
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        UPDATE session SET
            access_token     = 'test_token_001',
            token_type       = 'Bearer',
            token_expires_at = '2099-12-31T23:59:59',
            user_first_name  = '<actual_first_name>',
            user_last_name   = '<actual_last_name>'
        WHERE session_id = 1
        """
    )
    conn.commit()
    conn.close()
    return _load_backend(db)
```

Replace placeholder values (`<actual_user_id>`, `<actual_username>`) with real data from `world.db`. Query the `users` table (or equivalent) to find a valid test user.

## 6 Required Test Categories

### Category 1: Smoke Tests (2-3 tests)

Verify that the backend loads and provides basic state.

```python
class TestSmoke:
    def test_load_unauthenticated(self, backend):
        """Backend loads without errors in default state."""
        state = backend.visible_state()
        assert isinstance(state, dict)

    def test_load_authenticated(self, auth_backend):
        """Backend loads with pre-authenticated context."""
        state = auth_backend.visible_state()
        assert isinstance(state, dict)

    def test_visible_state_structure(self, auth_backend):
        """visible_state() returns expected top-level keys."""
        state = auth_backend.visible_state()
        # Verify keys that should always be present
        # Adapt based on actual backend implementation
```

Adapt the key assertions to match the actual `visible_state()` output of the backend.

### Category 2: Tool Coverage (1 test per tool minimum)

Call each tool with valid arguments and verify success. Every tool in `tools.jsonl` must have at least one dedicated test.

```python
class TestToolCoverage:
    def test_search_flights(self, auth_backend):
        """search_flights returns available flights."""
        result = auth_backend.call_tool(
            "search_flights",
            {"travel_from": "New York", "travel_to": "London", "travel_date": "2025-06-15"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)

    def test_book_flight(self, auth_backend):
        """book_flight creates a booking."""
        # May need to call prerequisite tools first
        result = auth_backend.call_tool("book_flight", {
            "flight_id": "...",  # Use actual data from world.db
            "card_id": "...",
        })
        assert result["success"] is True
        assert "booking_id" in result["data"]
```

Rules:
- Use actual data values from `world.db`, not invented ones. Query the database to find valid IDs, names, and values.
- If a tool requires output from another tool (per `tool_graph.json` edges), call the prerequisite tool first and use its return value.
- For authentication-required tools, use the `auth_backend` fixture.
- For tools that work without auth, use the `backend` fixture and verify they work.

### Category 3: Workflow Tests (2+ tests)

Execute multi-step tool chains derived from `tool_graph.json` edges. Verify state changes at each step.

```python
class TestWorkflows:
    def test_full_booking_workflow(self, auth_backend):
        """Complete booking flow: register card -> search -> book -> get invoice."""
        # Step 1: Register a credit card
        reg = auth_backend.call_tool("register_credit_card", {...})
        assert reg["success"] is True
        card_id = reg["data"]["card_id"]

        # Step 2: Search for flights
        search = auth_backend.call_tool("search_flights", {...})
        assert search["success"] is True
        flight_id = search["data"][0]["flight_id"]

        # Step 3: Book the flight
        book = auth_backend.call_tool("book_flight", {
            "flight_id": flight_id,
            "card_id": card_id,
        })
        assert book["success"] is True
        booking_id = book["data"]["booking_id"]

        # Step 4: Retrieve invoice
        invoice = auth_backend.call_tool("retrieve_invoice", {
            "booking_id": booking_id,
        })
        assert invoice["success"] is True

    def test_booking_and_cancellation(self, auth_backend):
        """Book a flight then cancel it, verifying refund."""
        # ... multi-step with assertions at each stage
```

Rules:
- Each workflow test must follow a valid path through `tool_graph.json` edges.
- Assert intermediate results, not just the final outcome.
- Verify state changes (e.g., balance decreased after booking, increased after cancellation).
- Include at least one workflow with 4+ steps.

### Category 4: Error Case Tests (4+ tests)

Call tools with invalid state or arguments and verify proper error handling.

```python
class TestErrorCases:
    def test_book_without_auth(self, backend):
        """Booking without authentication fails."""
        result = backend.call_tool("book_flight", {"flight_id": "F1", "card_id": "C1"})
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_REQUIRED"

    def test_book_nonexistent_flight(self, auth_backend):
        """Booking a non-existent flight fails."""
        result = auth_backend.call_tool("book_flight", {
            "flight_id": "NONEXISTENT",
            "card_id": "...",
        })
        assert result["success"] is False
        assert result["error"]["code"] == "FLIGHT_NOT_FOUND"

    def test_insufficient_balance(self, auth_backend):
        """Booking with insufficient card balance fails."""
        # Set up a scenario where balance is too low
        ...
        assert result["success"] is False
        assert result["error"]["code"] == "INSUFFICIENT_BALANCE"

    def test_invalid_parameter(self, auth_backend):
        """Tool call with invalid parameter value fails gracefully."""
        result = auth_backend.call_tool("search_flights", {
            "travel_date": "not-a-date",
        })
        assert result["success"] is False
```

Cover these error categories at minimum:
- **Authentication failures**: calling auth-required tools without auth
- **Not-found errors**: referencing non-existent IDs
- **Insufficient resources**: operations that exceed limits (balance, capacity)
- **Invalid inputs**: malformed parameters, out-of-range values

### Category 5: Protocol Compliance (1 test)

Verify that the backend follows the unified return format contract for every tool.

```python
class TestProtocol:
    def test_all_tools_handle_empty_args(self, auth_backend):
        """Every tool handles empty arguments without raising exceptions."""
        tool_names = []
        with open(ENV_ROOT / "spec" / "tools.jsonl") as f:
            for line in f:
                tool_names.append(json.loads(line)["name"])

        for tool_name in tool_names:
            result = auth_backend.call_tool(tool_name, {})
            assert isinstance(result, dict), f"{tool_name} did not return a dict"
            assert "success" in result, f"{tool_name} missing 'success' key"

    def test_unknown_tool(self, auth_backend):
        """Calling a non-existent tool returns an error, not an exception."""
        result = auth_backend.call_tool("nonexistent_tool_xyz", {})
        assert isinstance(result, dict)
        assert result["success"] is False
```

### Category 6: DB Isolation (1 test)

Verify that test operations on the copied database do not affect the original `world.db`.

```python
class TestDBIsolation:
    def test_original_db_unchanged(self, auth_backend, tmp_path):
        """Mutations on the session copy do not affect the original world.db."""
        original_path = ENV_ROOT / "world" / "world.db"
        original_hash = hashlib.md5(original_path.read_bytes()).hexdigest()

        # Perform some mutations
        auth_backend.call_tool("register_credit_card", {...})
        # ... more mutations

        after_hash = hashlib.md5(original_path.read_bytes()).hexdigest()
        assert original_hash == after_hash, "Original world.db was modified"
```

## Assertion Patterns

Use these patterns consistently throughout all tests:

**Success assertions:**
```python
assert result["success"] is True
assert result["data"]["booking_id"].startswith("BK")
assert isinstance(result["data"]["items"], list)
assert len(result["data"]["items"]) > 0
```

**Error assertions:**
```python
assert result["success"] is False
assert result["error"]["code"] == "BOOKING_NOT_FOUND"
assert "message" in result["error"]
```

**State change assertions:**
```python
# Before
state_before = auth_backend.visible_state()
balance_before = ...

# Action
auth_backend.call_tool(...)

# After
state_after = auth_backend.visible_state()
balance_after = ...
assert balance_after == balance_before - cost
```

## Minimum Test Count

Aim for 25-35 tests per environment. Distribution guideline:
- Smoke: 2-3
- Tool coverage: 1 per tool (typically 10-20)
- Workflows: 2-4
- Error cases: 4-8
- Protocol: 1-2
- DB isolation: 1

## Data Requirements

- All test data must come from `world.db`. Query the database to find valid user IDs, flight IDs, card numbers, etc.
- Do not invent data values. If a test needs a flight from New York to London, verify that such a route exists in the database.
- For workflow tests that create new records, use the return values from prerequisite tool calls rather than hardcoding IDs.

## Important

- Read `backend.py` to understand the exact method signatures (`call_tool`, `visible_state`) and return format before writing tests.
- Read `tools.jsonl` to get every tool name and its parameters. Every tool must have at least one test.
- Read `tool_graph.json` edges to design workflow tests that follow valid paths.
- Read `schema.sql` and query `world.db` for test data values.
- Tests must be runnable with `pytest tests/test_runtime.py` from the environment directory.
- Do not import from the `grafting` package. Use only the dynamic loading pattern shown above.
- Each test should have a clear docstring stating what it verifies.
