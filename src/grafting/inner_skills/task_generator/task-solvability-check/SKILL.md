---
name: task-solvability-check
description: Verify that every demand can be completed through conversation given the user's knowledge_boundary and the environment's tool set. Catches dead-lock scenarios where the user cannot provide information the tools require.
---

# Task Solvability Check

Verify that the task is solvable through a user-assistant
conversation. A task is solvable if, for every demand, there exists
a path from what the user knows (knowledge_boundary) to the tool
calls needed to fulfill the demand.

## Why This Matters

If a demand requires calling `get_user_details(user_id)` but the
user doesn't know their user_id and there's no search tool to find
it from their name, the conversation dead-locks: user can't provide
the ID, assistant can't look it up.

## Process

### 1. For Each Demand, Trace the Tool Chain

Starting from the subgraph's mutation tool, work backward through
prerequisite query tools:

```
update_reservation_flights
  ← needs reservation_id
    ← get_reservation_details(reservation_id)
      ← needs reservation_id
        ← get_user_details(user_id) returns reservation list
          ← needs user_id
            ← find_user_by_name(first_name, last_name)
              ← needs first_name, last_name ← USER KNOWS ✓
```

### 2. Check Each Input Parameter

For each tool in the chain, check where its required parameters
come from:

| Source | Solvable? |
|--------|-----------|
| Output of a previous tool in the chain | ✓ |
| Information in knowledge_boundary ("knows their name") | ✓ |
| Information naturally available (dates, cities, descriptions) | ✓ |
| Internal system ID with no lookup path | ✗ DEAD-LOCK |
| Credentials the user wouldn't know (OAuth secrets) | ✗ DEAD-LOCK |

### 3. Check Authentication Requirements

If any tool requires authentication (checks session for access_token
or similar):
- Is there a login/auth tool available?
- Does the user know credentials (username, name, email)?
- Or does world_setup pre-authenticate the session?

**If none of the above is true, the task is UNSOLVABLE.** The
conversation will dead-lock with the assistant asking for credentials
the user cannot provide. Fix by adding authentication SQL to
world_setup:

```sql
UPDATE session SET access_token = 'tok_xxx',
  token_expires_at = '2099-01-01T00:00:00'
  WHERE session_id = 1;
```

Or add credentials to knowledge_boundary:

```json
"knowledge_boundary": "Knows their email is alex@example.com and password is SecurePass!23"
```

Pre-authentication via world_setup is preferred for most tasks.
Credential-based auth in knowledge_boundary should only be used
for tasks that intentionally test the authentication flow.

### 4. Report

```
Demand 1: "Change flight to earlier date"
  Chain: find_user_by_name(name ← user knows) →
         get_user_details(user_id ← from find) →
         get_reservation(res_id ← from user details) →
         search_flight(origin, dest, date ← user knows) →
         update_flights(res_id, flight ← from search)
  Verdict: SOLVABLE ✓

Demand 2: "Check order status"
  Chain: get_order_details(order_id ← ???)
  User does not know order_id.
  No search_order_by_name tool exists.
  Verdict: DEAD-LOCK ✗
```

### 5. Fix or Fail

If a demand is unsolvable:
- **Check knowledge_boundary**: maybe the user SHOULD know this
  value. Update knowledge_boundary to include it.
- **Check tools**: maybe there IS a lookup tool you missed.
- **Add world_setup**: pre-set session state so the tool doesn't
  need the parameter.
- **Rewrite demand**: change to a demand that's solvable with
  available tools.
- **Last resort**: report failure.
