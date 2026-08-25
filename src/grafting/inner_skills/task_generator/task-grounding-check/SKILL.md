---
name: task-grounding-check
description: Verify that every entity referenced in demands exists in world.db or will exist after world_setup executes. Catches phantom references like non-existent files, users, or records.
---

# Task Grounding Check

Verify that the task's demands reference real entities. This catches
the class of bugs where demands mention files, users, or records
that don't exist in the environment.

## Why This Matters

If a demand says "archive app.log into logs/archive" but neither
`app.log` nor `logs/archive` exist in the file system, the task
is impossible. The assistant will waste turns searching for
non-existent resources.

## Process

### 1. Extract Entity References

For each demand, identify what concrete entities it references:

- **Stateful environments**: user names, product names, order
  descriptions, cities, dates, account references
- **File system environments**: file paths, directory names
- **Database environments**: table names, column names
- **Stateless environments**: typically no entity references (skip)

### 2. Verify Against world.db

Query `env/world/world.db` to check each entity exists:

- File paths: query the inode/file table for matching paths
- User names: query the users table for matching names
- Products/orders: query relevant tables
- Table/column names: check `sqlite_master` or schema

### 3. Check world_setup Creates Missing Entities

If an entity is NOT in world.db, check if `world_setup` SQL will
create it. Parse each INSERT/UPDATE statement to see what entities
it establishes.

### 4. Report

For each demand, report:

```
Demand 1: "Archive app.log into logs/archive"
  - app.log: NOT FOUND in world.db, NOT CREATED by world_setup ← FAIL
  - logs/archive: NOT FOUND, NOT CREATED ← FAIL

Demand 2: "Change flight to May 16"
  - User "Aarav Anderson": FOUND in users table ← OK
  - Flight on May 16: FOUND via search ← OK
```

### 5. Fix or Fail

If any entity is missing:
- **Preferred**: add world_setup SQL to create the entity
- **Alternative**: rewrite the demand to reference an entity that
  exists
- **Last resort**: report failure and let task-mutation handle it

After fixing, re-verify.

## Common Pitfalls

- File system environments: agent invents plausible-sounding paths
  without checking. ALWAYS query the file/inode table.
- Session tables: if a demand depends on session state (e.g.,
  current user), verify world_setup sets it.
- Date-dependent data: if a demand references "flights on May 16",
  verify that date has available flights in the DB.
