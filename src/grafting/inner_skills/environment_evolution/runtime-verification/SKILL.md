---
name: runtime-verification
description: After applying fixes, verify the environment backend works by running key tool calls against a test copy of world.db. Tests authentication flow, query tools, and mutation tools. Reports pass/fail for each test.
---

# Runtime Verification

Verify the environment backend works correctly after fixes.

## Inputs

- `env/runtime/backend.py` — backend to verify
- `env/world/world.db` — seed database
- `env/world/schema.sql` — schema reference
- `env/spec/tools.jsonl` — tool definitions
- `env/spec/tool_graph.json` — tool dependencies

## Process

Run the verification script:

```bash
python scripts/verify_tool_calls.py \
  --env-dir env \
  --output output/verification_result.json
```

The script will:

1. Copy `world.db` to a temporary test database
2. Dynamically load `EnvironmentBackend` from `backend.py`
3. Parse `tools.jsonl` for tool names and required parameters
4. For each tool, construct minimal valid arguments:
   - String params: empty string `""`
   - Integer params: `0`
   - Float params: `0.0`
   - Object params: `{}`
   - Array params: `[]`
   - Boolean params: `false`
5. Call `backend.call(tool_name, args)` and check the result:
   - `INTERNAL_ERROR` → **FAIL** (backend bug)
   - `UNKNOWN_TOOL` → **FAIL** (missing handler)
   - Any other error (AUTH_FAILED, NOT_FOUND, VALIDATION_ERROR) → **PASS** (expected business logic error)
   - `success: true` → **PASS**
6. Call `visible_state()` and verify it returns a dict without crashing
7. Clean up temporary database

## Interpreting Results

- **All PASS**: Environment backend is healthy
- **Any FAIL with INTERNAL_ERROR**: Backend has bugs — go back to `backend-bugfix`
- **Any FAIL with UNKNOWN_TOOL**: Tool registered in tools.jsonl but not implemented — go back to `backend-bugfix`

Note: AUTH_FAILED, NOT_FOUND, VALIDATION_ERROR are NOT failures — they are correct business logic responses to minimal/invalid arguments.

## Output

`output/verification_result.json`:

```json
{
  "total_tools": 30,
  "tested": 30,
  "passed": 28,
  "failed": 2,
  "visible_state_ok": true,
  "errors": [
    {
      "tool_name": "add_text",
      "error_code": "INTERNAL_ERROR",
      "error_message": "invalid literal for int()",
      "arguments": {"api_key": "", "draft_id": "", "content": ""}
    }
  ]
}
```
