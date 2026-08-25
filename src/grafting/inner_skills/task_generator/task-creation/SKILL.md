---
name: task-creation
description: Generate a task.json draft from sampled subgraphs, rule, and persona. The subgraphs collectively define the scope of a single demand. Queries world.db for concrete data to ground demands and constructs world_setup SQL for environment initialization.
---

# Task Creation

Given the subgraphs from tool-graph-exploration, the rule, and the
persona, generate a complete task.json draft.

## Inputs

- Subgraph list from tool-graph-exploration (in working context)
- Rule and persona from entry prompt
- `env/spec/ENVIRONMENT.md` — environment description
- `env/spec/tools.jsonl` — tool definitions
- `env/world/schema.sql` — database structure
- `env/world/world.db` — queryable world data

## Process

### 1. Understand the Environment

Read `ENVIRONMENT.md` to understand:
- What domain this environment simulates
- What session initialization is needed (documented in Session
  Initialization section)
- What workflows are typical

Read `schema.sql` to understand table structures.

### 2. Query World Data and Assess Diversity

Use `sqlite3 env/world/world.db` to understand what data exists:

- Count entities per table and per status
- Identify which entities are already referenced by existing
  tasks in the pool (query tasks.sqlite3)
- Note any tables with very few rows

**Important**: Do NOT limit yourself to existing data. If the
environment has only 2 orders or 3 customers, you will end up
creating tasks that reference the same entities as previous
tasks. Instead, plan to INSERT new entities via world_setup
(step 4) to create a unique scenario for this task.

Strategy:
- **Reuse existing entities** when they naturally fit and haven't
  been overused by prior tasks in the pool
- **Create new entities via world_setup** when existing data is
  sparse, already used by other tasks, or when the demand needs
  specific preconditions (e.g., a user with a particular balance,
  an order in a specific status, a reservation on a particular date)

### 3. Construct the Demand

Combine the sampled subgraphs into **one single demand string**.
The demand should be complex enough to require 2-5 user-assistant
turns (target average ~3) and multiple tool calls.

Write the demand as a natural paragraph describing what the user
wants to accomplish — the **goals and desired outcomes**, not the
implementation steps. The demand should specify *what* the user
wants to achieve, not *how* to achieve it. The assistant should
be free to choose the most efficient path.

- ✓ "Investigate the dependency chain of the HTTP client in the
    beacon-web repository, generate a dependency report and
    architecture diagram, and create an investigation note
    summarizing coupling risks."
- ✗ "List the available repositories to find beacon-web, set it
    as the active repository, then search for files related to
    'client', get the file details, pull a dependency report..."

List the sub-goals in a logical order that
a real user would follow. Example:

  "I need to check my reservation details for the upcoming flight
  from New York to Phoenix, correct my co-passenger Yusuf Taylor's
  date of birth to 1950-11-26, look into whether there are cheaper
  flight options for the same route on an earlier date, and add two
  extra checked bags charged to my Visa card."

Rules:
- Must NOT contain tool function names (e.g., `update_reservation`)
- MAY contain parameter values a real user would know
- Should involve 3-5 distinct operations that map to the sampled
  mutation tools
- Include operations that naturally require discovery or clarification
  (e.g., "look into options", "find the best price", "check if available")

For `miss_func` rule: include 1-2 sub-goals for capabilities that
do NOT exist in `tools.jsonl`.

### 4. Construct world_setup

world_setup is your primary tool for creating unique task
scenarios. It runs BEFORE the simulation starts, so you can
set up any preconditions the demand needs.

**Session initialization** (REQUIRED for authenticated environments):
- If the environment has an authentication tool (e.g.,
  `authenticate`, `login`, `authenticate_user`), world_setup
  MUST either:
  (a) pre-set the session with a valid access_token so tools
      work without an auth conversation, OR
  (b) include the user's credentials in knowledge_boundary
      so the user can authenticate during the conversation.
- Option (a) is strongly preferred — it avoids wasting turns
  on authentication and focuses the trajectory on the actual
  demand. Use option (b) only for `error_recovery` rule tasks
  that specifically test auth failure/retry scenarios.
- Check ENVIRONMENT.md's Session Initialization section for
  the exact SQL pattern and valid token values.

**Entity creation** (use liberally for diversity):
- INSERT new users, customers, orders, reservations, etc.
  that the demand will reference
- This is how you avoid reusing the same entities across tasks
- Read schema.sql carefully for required columns, constraints,
  and valid enum values
- Use realistic but unique values (different names, different
  amounts, different dates than existing data)

**Precondition setup** (for specific scenarios):
- Adjust balances, statuses, or timestamps to create the exact
  starting state the demand needs
- For error_recovery: set up states that will trigger errors
  (e.g., insufficient balance, expired items)

Example — creating a fresh scenario for a trading task:
```sql
UPDATE session SET access_token='tok_001', username='alex',
  token_expires_at='2099-01-01T00:00:00' WHERE session_id=1;
INSERT INTO orders (order_id, order_type, symbol, price, amount, status)
  VALUES (88801, 'Buy', 'NVDA', 220.00, 10, 'Pending');
INSERT INTO orders (order_id, order_type, symbol, price, amount, status)
  VALUES (88802, 'Sell', 'TSLA', 670.00, 5, 'Pending');
```

Now the demand can ask to cancel one order, modify another,
and place a new one — all referencing entities that no other
task uses.

All SQL must be valid against `schema.sql`. Use the exact table
and column names from the schema.

### 5. Construct user_agent_config

```json
{
  "role": "realistic role matching the environment domain",
  "personality": "behavioral traits; must include at least one clarification-producing trait",
  "knowledge_boundary": "explicit knows / does-not-know statement"
}
```

The `knowledge_boundary` describes what the user **knows about the
world** — NOT what the user **wants to do**. This distinction is
critical:

- ✅ "Knows their name is Aarav Anderson"
- ✅ "Knows co-passenger Yusuf Taylor's correct DOB is 1950-11-26"
- ✅ "Does not know system user_id or payment method IDs"
- ❌ "Wants 2 extra bags" — this is a demand, not knowledge
- ❌ "Expects a $100 travel certificate" — this is a demand
- ❌ "Wants to cancel the reservation" — this is a demand

**ID knowledge rule**: Decide whether the user would naturally
know a given ID by asking: "Would a real person in this role
have this identifier from a prior interaction (confirmation
email, receipt, ticket notification, file path)?"

- ✅ User knows a booking confirmation code they received by email
- ✅ User knows a support ticket number from a notification
- ✅ User knows file paths they work with daily
- ✅ User knows key names in a KV store they manage
- ❌ User knows a database-internal user_id like `emma_kim_4489`
- ❌ User knows a payment_method_id like `credit_card_3786623`

When a user knows an ID, the assistant can skip discovery and
go straight to the detail lookup — this is realistic and fine.
When a user does NOT know an ID, the assistant must use search/
lookup tools first — this produces a richer discovery path.

For trajectory diversity, prefer a mix: some tasks where the
user knows key IDs (shorter, more direct) and some where they
don't (longer, more discovery steps).

User goals belong in the `demand` field. If knowledge_boundary
duplicates demand content, the user agent may behave inconsistently.

Additional rules:
- If a demand mentions a name, the user must know that name
- The user must NOT know internal system IDs
- The user should know enough to START the conversation but rely
  on the assistant to discover system-level details
- `role` should be a short role description (1 line), not a
  detailed scenario with specific reservation numbers

### 6. Assemble task.json

```json
{
  "demand": "Fix the DOB error for Yusuf Taylor, add 2 extra bags, and issue a $100 certificate for the inconvenience",
  "world_setup": [...],
  "user_agent_config": {
    "role": "...",
    "personality": "...",
    "knowledge_boundary": "..."
  }
}
```

Do NOT include `init_context` — it is deprecated.

## Output

Write the draft to `output/task.json`. Then proceed to the
validation skills.
