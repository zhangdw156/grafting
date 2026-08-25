---
name: world-setup-validation
description: Validate that world_setup SQL statements execute successfully against a copy of world.db. Verifies SQL syntax, schema compatibility, and that entities referenced by demands exist after execution.
---

# World Setup Validation

Execute the task's world_setup SQL against a temporary copy of
world.db to verify correctness.

## Process

### 1. Copy the Database

```bash
cp env/world/world.db /tmp/validation_world.db
```

### 2. Execute world_setup Statements

Run each SQL statement in order:

```bash
sqlite3 /tmp/validation_world.db "<statement_1>"
sqlite3 /tmp/validation_world.db "<statement_2>"
...
```

If ANY statement fails, report the error and the failing SQL.

### 3. Verify Post-Setup State

After all statements execute, verify that entities referenced
by demands exist in the database:

- If demands reference a user: query the users table
- If demands reference a file: query the file/inode table
- If demands reference a session state: query the session table
- If world_setup creates a constraint for error_recovery: verify
  the constraint exists

**Authentication check**: If the environment has tools that require
an access_token (check tools.jsonl for `access_token` parameters),
verify that after world_setup execution the session table has a
non-NULL access_token. If not, and the user's knowledge_boundary
does not include login credentials, the task will dead-lock during
simulation. This is a FAIL — add authentication SQL to world_setup.

### 4. Clean Up

```bash
rm /tmp/validation_world.db
```

### 5. Report

```
Statement 1: UPDATE session SET current_user_id = 'aarav_anderson_6237'
  Result: OK ✓

Statement 2: INSERT INTO logs (path, content) VALUES ('/logs/app.log', '')
  Result: OK ✓

Post-setup verification:
  - session.current_user_id = 'aarav_anderson_6237' ✓
  - /logs/app.log exists in inode table ✓

World setup validation: PASSED
```

## Common Failures

- Table or column name typo (check against schema.sql)
- INSERT violates a NOT NULL or UNIQUE constraint
- Referenced foreign key does not exist
- Wrong data types (e.g., string where integer expected)

On failure: fix the SQL statement and re-validate. Do NOT proceed
to task-pool-conflict-check with invalid world_setup.
