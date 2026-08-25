---
name: demand-verification
description: Verify that all sub-goals in the demand were achieved by querying the post-replay database state. Uses SQL queries against eval.db to check each state change.
---

# Demand Verification

After trajectory replay, verify that the demand was actually
fulfilled by checking the database state.

## Inputs

- `output/replay_result.json` — replay results (from trajectory-replay)
- `output/eval.db` — post-replay database
- The demand string from the trajectory's embedded task
- `env/world/schema.sql` — database structure reference

## Process

### 1. Read the Demand

Extract the demand from the trajectory. It describes what the
user wanted to accomplish.

### 2. Decompose into Sub-Goals

Break the demand into discrete, verifiable sub-goals. Each
sub-goal should map to a concrete state change in the database.

Example demand:
> "I need to update my companion's DOB to 1968-08-13,
> change to the May 16 business-class flight, and add
> 2 extra paid checked bags"

Sub-goals:
1. Passenger DOB updated to 1968-08-13
2. Reservation changed to May 16 flight
3. Reservation cabin changed to business
4. Total baggages = 4, nonfree_baggages = 2

### 3. Write Verification Queries

For each sub-goal, write a SQL query against `output/eval.db`
to check whether the expected state change occurred.

```bash
sqlite3 output/eval.db "SELECT dob FROM reservation_passengers WHERE first_name = 'Raj' AND last_name = 'Brown';"
```

Read `env/world/schema.sql` to understand table and column names.

### 4. Execute and Verify

Run each query and compare the result against the expected value.

### 5. Report

For each sub-goal, report:
- The sub-goal description
- The SQL query used
- The actual result
- Whether it matches the expected state: fulfilled / not_fulfilled

```
Sub-goal 1: Passenger DOB updated to 1968-08-13
  Query: SELECT dob FROM reservation_passengers WHERE ...
  Result: 1968-08-13
  Status: fulfilled

Sub-goal 2: Reservation flight date changed to May 16
  Query: SELECT date FROM reservation_flights WHERE ...
  Result: 2024-05-16
  Status: fulfilled
```

If ALL sub-goals are fulfilled, set `demand_fulfilled = true`.
If ANY sub-goal is not fulfilled, set `demand_fulfilled = false`
and record an issue with type `demand_not_fulfilled`.

## Edge Cases

- **miss_func rule tasks**: The task's `rule` field will be set to
  `"miss_func"`. The demand intentionally includes sub-goals that
  cannot be fulfilled because the required tool is missing from the
  environment. For these sub-goals, check whether the assistant
  correctly identified the limitation and communicated it to the
  user. If so, the sub-goal is considered **handled correctly** —
  do NOT mark it as `demand_not_fulfilled`. Only mark it as
  `demand_not_fulfilled` if the assistant falsely claimed to have
  completed the action or silently ignored the request.

- **error_recovery rule tasks**: The demand may involve triggering
  an error then recovering. Verify the final state after recovery,
  not the intermediate error state. If the assistant encounters a
  system-imposed obstacle (e.g., duplicate name, deleted resource)
  and the user explicitly agrees to a modified approach (e.g., an
  alternative title, a different workaround), the modified outcome
  satisfies the demand — do NOT mark it as `demand_not_fulfilled`.

- **miss_param rule tasks**: The demand intentionally omits some
  information. The assistant is expected to ask the user or make
  reasonable inferences. Verify that the final outcome matches the
  user's stated intent, even if the assistant had to fill gaps.

- **Formatting tolerance**: When comparing expected values against
  database state, ignore trivial formatting differences that do not
  change the semantic meaning:
  - Trailing punctuation from natural conversation (e.g., a period
    at the end of a string that was part of the user's sentence
    syntax, not the demanded content)
  - Whitespace normalization (leading/trailing spaces)
  - Case differences in free-text fields (not in IDs or enum values)
  These are artifacts of natural language conversation, not failures
  to fulfill the demand.
