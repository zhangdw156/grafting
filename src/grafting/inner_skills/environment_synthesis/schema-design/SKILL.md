---
name: schema-design
description: Guide for designing schema.sql and populating world.db for Grafting packaged environments. Covers table design principles (reference vs. mutable tables), naming conventions, common patterns (session table, reference lookups, transaction tables, junction tables), data richness requirements, schema.sql formatting, and world.db population guidelines.
---

# Schema Design

This skill provides the complete ruleset for designing the database schema (`schema.sql`) and populating the seed database (`world.db`) for a packaged environment. The database is the single source of truth for all environment state. Every tool reads from it, every mutation writes to it, and session isolation depends on it.

## Sandbox Paths

- Read tool definitions from: `output/<env_name>/spec/tools.jsonl`
- Write schema to: `output/<env_name>/world/schema.sql`
- Write seed database to: `output/<env_name>/world/world.db`

## Table Design Principles

### Two table types

Every table in the schema falls into one of two categories:

- **Reference tables** (read-only): tools never INSERT, UPDATE, or DELETE rows in these tables. They contain static data seeded during synthesis: airport codes, currency exchange rates, product catalogs, route definitions, configuration lookups. Their contents are identical at the start and end of every session.

- **Mutable tables**: at least one mutation tool writes to these tables. They contain bookings, transactions, registered credit cards, session state, user-created resources. Their contents change during a session.

The distinction is determined entirely by whether any tool in the environment writes to the table. There is no explicit flag or annotation in the schema -- the tool_graph.json `writes` arrays are the authoritative source.

### Why this matters

Reference tables can contain large amounts of seed data without concern about state management. Mutable tables require careful thought about initial state, because the trajectory planner may need pre-existing records to create interesting tasks (e.g., "cancel this existing booking").

## Naming Conventions

### Tables

Use `snake_case`. Name tables after the plural form of the entity they store.

Good: `flight_routes`, `credit_cards`, `insurance_policies`, `exchange_rates`
Bad: `FlightRoutes`, `CreditCard`, `tbl_insurance`, `exchange_rate_data`

### Columns

Use `snake_case`. Be descriptive but concise.

Good: `card_number`, `travel_date`, `base_cost_usd`, `token_expires_at`
Bad: `cardNum`, `travelDate`, `cost`, `expiry`

### Primary keys

Use `<entity>_id` as the primary key column name.

Good: `booking_id`, `card_id`, `insurance_id`, `airport_id`
Bad: `id`, `pk`, `booking_pk`, `idx`

### Foreign keys

Declare foreign keys explicitly in the CREATE TABLE statement:

```sql
CREATE TABLE bookings (
    booking_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    -- other columns...
    FOREIGN KEY (card_id) REFERENCES credit_cards(card_id)
);
```

### CHECK constraints for enum columns

Use CHECK constraints for columns that accept a fixed set of values:

```sql
travel_class TEXT NOT NULL CHECK(travel_class IN ('economy', 'business', 'first'))
```

This provides a second layer of validation beyond the tool's `inputSchema` enum. The backend can rely on the CHECK constraint to catch invalid values that bypass tool-level validation.

## Common Patterns

### Pattern 1: Single-row session table

Every environment that uses authentication needs a session table. It holds auth state, user identity, and session-level configuration.

```sql
CREATE TABLE session (
    session_id INTEGER PRIMARY KEY DEFAULT 1,
    access_token TEXT,
    token_type TEXT,
    token_expires_at TEXT,
    token_scope TEXT,
    user_first_name TEXT,
    user_last_name TEXT,
    budget_limit REAL
);
```

Key rules:
- Primary key is `session_id INTEGER PRIMARY KEY DEFAULT 1`. There is always exactly one row.
- Auth columns (`access_token`, `token_expires_at`) start as NULL for unauthenticated sessions.
- Session state (authentication, user identity, etc.) is set via `world_setup` SQL in `task.json` before the backend is instantiated.
- This is a mutable table (the authenticate tool writes to it).

### Pattern 2: Reference lookup tables

Static data that tools read but never modify. Seed these with comprehensive data.

```sql
CREATE TABLE airports (
    iata_code TEXT PRIMARY KEY,
    city_name TEXT NOT NULL
);

CREATE TABLE flight_routes (
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    base_cost_usd INTEGER NOT NULL,
    PRIMARY KEY (origin, destination),
    FOREIGN KEY (origin) REFERENCES airports(iata_code),
    FOREIGN KEY (destination) REFERENCES airports(iata_code)
);

CREATE TABLE exchange_rates (
    base_currency TEXT NOT NULL,
    target_currency TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (base_currency, target_currency)
);
```

### Pattern 3: Transaction tables

Records created or modified by mutation tools during a session.

```sql
CREATE TABLE bookings (
    booking_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    travel_from TEXT NOT NULL,
    travel_to TEXT NOT NULL,
    travel_class TEXT NOT NULL CHECK(travel_class IN ('economy', 'business', 'first')),
    travel_cost REAL NOT NULL,
    transaction_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (card_id) REFERENCES credit_cards(card_id)
);
```

Key rules:
- Primary key is a generated ID (e.g., `BK0000001`), not an auto-increment integer. This ensures deterministic ID generation via the backend's `_next_id` helper.
- Include foreign keys to reference the entity that owns or created the record.
- Include CHECK constraints for enum columns.

### Pattern 4: Junction tables

For many-to-many relationships.

```sql
CREATE TABLE booking_passengers (
    booking_id TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    seat_class TEXT NOT NULL,
    PRIMARY KEY (booking_id, passenger_id),
    FOREIGN KEY (booking_id) REFERENCES bookings(booking_id),
    FOREIGN KEY (passenger_id) REFERENCES passengers(passenger_id)
);
```

### Pattern 5: Self-referential tree table

For hierarchical data like file systems, organization structures, or category trees. A single table with a `parent_id` foreign key referencing itself.

```sql
CREATE TABLE inodes (
    inode_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER REFERENCES inodes(inode_id),
    name       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK(type IN ('file', 'dir')),
    content    TEXT DEFAULT '',
    size       INTEGER DEFAULT 0,
    modified_at TEXT NOT NULL,
    UNIQUE(parent_id, name)
);
```

Key rules:
- Root node has `parent_id = NULL`.
- Path resolution walks `parent_id` links: downward via `SELECT ... WHERE parent_id = ? AND name = ?`, upward via recursive parent lookup.
- `UNIQUE(parent_id, name)` prevents duplicate names in the same directory.
- Recursive operations (delete subtree, copy subtree, compute size) traverse the tree via SQL.

### Pattern 6: Single-entity state table

For environments that simulate a single controllable object (vehicle, robot, device). All state attributes are columns in one row.

```sql
CREATE TABLE vehicle_state (
    vehicle_id      INTEGER PRIMARY KEY DEFAULT 1,
    engine_state    TEXT NOT NULL DEFAULT 'stopped' CHECK(engine_state IN ('running', 'stopped')),
    fuel_level      REAL NOT NULL DEFAULT 10.0,
    battery_voltage REAL NOT NULL DEFAULT 12.6,
    speed           REAL NOT NULL DEFAULT 0.0,
    cruise_active   INTEGER NOT NULL DEFAULT 0,
    cruise_speed    REAL NOT NULL DEFAULT 0.0
);
```

Key rules:
- Exactly one row, `PRIMARY KEY DEFAULT 1`, same pattern as the session table.
- Every tool reads and/or writes columns in this single row.
- Use CHECK constraints for enum-like state fields.
- May be paired with subsidiary tables (e.g., a `doors` table with one row per door).

## Data Sourcing Strategies

### Strategy 1: Direct fixture conversion

When the source environment has a small fixture (<100KB), convert it entirely into `world.db`. Every record in the fixture becomes a row.

### Strategy 2: Large fixture subsetting

When the source fixture is large (>500KB), extract a representative subset. The goal is a `world.db` of 50-500KB that still covers all important data patterns.

Guidelines:
- Keep all reference data (products, airports, routes) — these are usually not the size problem.
- Subset mutable/transactional data: e.g., 20 users out of 500, 50 orders out of 2000.
- Ensure the subset covers all status values, edge cases, and relationship types.
- Verify that foreign key relationships are intact after subsetting (no dangling references).

### Strategy 3: Variant fixture merging

When the source environment has multiple scenario variants (default.json, variant_02.json, variant_07.json, etc.), merge them into one rich `world.db` rather than keeping separate scenarios.

Guidelines:
- Union all unique records across variants.
- Deduplicate by primary key (keep the most data-rich version if there are conflicts).
- The result should have more records and more variety than any single variant alone.
- `world_setup` SQL in `task.json` replaces variant selection — the runtime executes SQL statements to configure session state before the backend is instantiated, instead of loading different fixtures.

## Data Richness Requirements

The quality of the environment directly depends on the richness of its seed data. Sparse data produces boring, repetitive tasks.

### Language: English only

All seed data, column names, comments, and string values in schema.sql and world.db must be in English. Even when the raw skill originates from a non-English source, translate all content to English. Do not create bilingual columns (e.g., `name_zh`/`name_en`) — use a single English column (e.g., `name`).

### Reference tables: comprehensive variety

Reference tables must contain enough data to support diverse tasks. Guidelines:

- Airport/location tables: 20+ entries covering multiple regions/countries
- Route/connection tables: 100+ entries covering a variety of origin-destination pairs, including some with no direct route (to test error handling)
- Currency/exchange rate tables: 10+ currency pairs
- Product catalog tables: 30+ items across multiple categories
- Category/type lookup tables: all valid values represented

The data should feel like a realistic subset of a production system, not a toy example with 3 airports.

### Mutable tables: realistic pre-seeded records

Mutable tables must be pre-seeded with enough records to support
diverse task generation. An empty or near-empty mutable table
forces every task to reference the same entities, producing
repetitive trajectories.

**Hard minimums:**
- Entity tables that mutation tools operate on (orders, issues,
  reservations, tickets, claims): **at least 8-15 records**
- Each status/state value must have **at least 3 records**
  (e.g., 3+ pending orders, 3+ completed orders, 3+ cancelled
  orders — not just 1 of each)
- User/account tables: **at least 5 records** with different
  profiles (names, balances, membership tiers)
- Payment methods / credentials: **3-5 per user** with varying
  types and balances

**Why these minimums matter:** The task generator samples different
subgraphs for each task, but if all `cancel_order` tasks must
target the same 1 pending order, the demands end up identical.
With 5+ pending orders involving different users, products, and
amounts, each cancel task can reference a different entity.

**Status distribution should mirror reality:**
- Most orders/issues should be in terminal states (completed,
  resolved) — these form the history the user can reference
- A meaningful fraction should be in actionable states (pending,
  open) — these are what mutation tools operate on
- A few should be in exceptional states (cancelled, on_hold,
  waiting_on_customer) — these support edge-case tasks

### Edge cases in seed data

Include data that tests boundary conditions:

- A credit card with near-zero balance (e.g., $0.50)
- A booking on today's date (to test date boundary logic)
- An expired item or record (if the domain has time-sensitive data)
- A record with the maximum allowed value for a constrained field
- Entries with special characters in string fields (names with hyphens, apostrophes)

### Minimum data volume

A `world.db` with fewer than ~50 total rows across all tables is
almost always too sparse. Aim for **80-200+ rows** as a healthy
range for most environments.

Minimum viable seed data rules:
- For entity tables (users, projects, orders, issues, etc.):
  seed **at least 8 records** per table
- For relationship tables (assignments, memberships, comments):
  seed **at least 10 records**
- For reference/lookup tables (products, categories, airports):
  seed **at least 15 records**
- Multiple user profiles: seed **at least 5 distinct users**
  with different attributes so tasks can target different people
- After populating, run a self-check: for each mutation tool,
  verify that its target tables have enough rows in the right
  statuses to support **at least 5 different task scenarios**
  without reusing the same entity

### Data entropy and diversity

Row count alone does not guarantee task diversity. Columns with >80% of rows sharing the same value ("low entropy") make multi-scenario task generation impossible. After populating `world.db`, audit every non-PK column and fix any that violate the rules below.

**Status and state columns must cover the full lifecycle.**
If a table has a `status` column with values like `pending / active / completed / cancelled`, seed data must include rows in **every** meaningful state. An `orders` table where all 50 rows have `status = NULL` or `status = 'active'` is useless — the task generator cannot create "cancel an order" or "review a completed order" tasks. Aim for no single status value exceeding 60% of rows.

**Nullable columns must have a realistic mix of NULL and non-NULL.**
Columns like `cancelled_reason`, `delivered_at`, `retry_of_task_id` are NULL for most rows by design (most orders are not cancelled, most tasks do not retry). This is acceptable **only if** the non-NULL values exist in sufficient quantity for task generation — at least 2-3 rows with non-NULL values. A column that is 100% NULL is a dead column; one that is 100% non-NULL in a naturally-nullable field is unrealistic. Target 15-30% non-NULL for optional fields that drive task scenarios.

**Foreign key columns must reference diverse parents.**
If every row in `issues` points to the same `project_id`, the environment effectively has one project. Distribute FK references across at least 3 different parent rows, ideally covering all parents.

**Boolean feature flags are a known exception.**
Columns like `supports_audio`, `is_bot`, `is_default` are structurally low-entropy — most components don't support audio, most users aren't bots, only one SLA is default. These are **not** defects. Do not pad boolean flags artificially. The quality-validation G-series checks skip `is_*` and `supports_*` prefixed columns.

**Single-entity FK columns are a known exception.**
Environments that model a single workspace, team, or deployment will have FK columns (e.g., `workspace_id`, `team_id`, `deployment_key`) with 100% the same value across a table. This is structural, not a defect. If the environment is designed as a single-tenant simulation, this is correct.

**Concrete anti-patterns from production environments (avoid these):**
- `tau2_airline`: 50 reservations all with `status = NULL`, `nonfree_baggages = 0`, `flight_type = 'one_way'`, `country = 'USA'` — quantity without diversity is worse than fewer rows with variety
- `mcpmark_github`: 29 tables but 19 completely empty, all data from a single repo — skeleton tables without data defeat the purpose of having them
- `tau2_retail`: 813 rows but `cancel_reason`, `exchange_items`, `return_items` all 100% NULL — transactional environments need pre-existing completed, cancelled, returned, and exchanged records
- `agentx_news`: profiles with 100% empty `avatar_url` and `banner_url` — if a field exists, some records should use it

### Domain-driven data volume

Different domains have naturally different data volumes. Do not pad data to hit a number — design data that makes sense for the domain:

- **Social platforms** (agentgram, posting_api, agentx_news): need 80-110+ rows. Multiple user profiles (5-8), an asymmetric follow graph (not everyone follows everyone), varied posts with tags/likes/comments, conversations with message history. The richness of the social graph directly determines task diversity.
- **File systems** (gorilla_file_system, mcpmark_filesystem): need 40-60 rows. A realistic directory tree 3-5 levels deep, files with actual text content (not empty) so grep/cat/sort/tail produce meaningful results, mix of file types (source code, configs, logs, docs).
- **E-commerce / booking** (travel_booking, tau2_retail, tau2_airline): need 100-800+ rows. Many reference entries (airports, products, routes) plus pre-seeded transactional records covering different statuses.
- **Memory / KV stores** (memory_kv, memory_vector): need 30-40 rows. Pre-seeded with realistic content reflecting an actual use case (e.g., engineering team context, project notes), not generic "test_key_1" placeholders.
- **Single-entity environments** (vehicle_control): naturally low volume (~30 rows). One row for the vehicle state, a few rows for subsystems (doors, tires). Do not pad with artificial data — the domain is inherently about one controllable object.
- **Telecom** (telnyx): need 40-60 rows. Message/call/fax history with different statuses (delivered, failed, queued), multiple phone numbers across area codes.

## schema.sql Format

### File structure

Group tables by type with section comments:

```sql
-- =============================================
-- Reference tables (read-only)
-- =============================================

CREATE TABLE airports (
    iata_code TEXT PRIMARY KEY,
    city_name TEXT NOT NULL
);

CREATE TABLE flight_routes (
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    base_cost_usd INTEGER NOT NULL,
    PRIMARY KEY (origin, destination),
    FOREIGN KEY (origin) REFERENCES airports(iata_code),
    FOREIGN KEY (destination) REFERENCES airports(iata_code)
);

-- =============================================
-- Mutable tables
-- =============================================

CREATE TABLE session (
    session_id INTEGER PRIMARY KEY DEFAULT 1,
    access_token TEXT,
    token_type TEXT,
    token_expires_at TEXT,
    token_scope TEXT,
    user_first_name TEXT,
    user_last_name TEXT,
    budget_limit REAL
);

CREATE TABLE credit_cards (
    card_id TEXT PRIMARY KEY,
    card_number TEXT NOT NULL UNIQUE,
    expiration_date TEXT NOT NULL,
    cardholder_name TEXT NOT NULL,
    card_verification_number INTEGER NOT NULL,
    balance REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE bookings (
    booking_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    travel_from TEXT NOT NULL,
    travel_to TEXT NOT NULL,
    travel_class TEXT NOT NULL CHECK(travel_class IN ('economy', 'business', 'first')),
    travel_cost REAL NOT NULL,
    transaction_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (card_id) REFERENCES credit_cards(card_id)
);
```

### Rules

- Every CREATE TABLE includes column types and constraints (NOT NULL, PRIMARY KEY, FOREIGN KEY, CHECK, UNIQUE, DEFAULT).
- The file must be executable: `sqlite3 new.db < schema.sql` must create a valid empty schema.
- No INSERT statements in `schema.sql`. All seed data lives exclusively in `world.db`.
- No DROP TABLE IF EXISTS. The schema file creates tables on a fresh database.
- Use TEXT for string columns, INTEGER for whole numbers, REAL for floating-point numbers. Avoid BLOB.
- Date/time values are stored as TEXT in ISO 8601 format (e.g., `"2024-06-15"`, `"2024-06-15T14:30:00"`).

## world.db Population

`world.db` is a SQLite database file that contains both the schema (all CREATE TABLE statements applied) and all seed data (INSERT statements applied).

### What it contains

- The complete schema from `schema.sql` (all tables created).
- All reference data: every airport, every route, every exchange rate, every product in the catalog.
- Pre-seeded mutable data: existing credit cards, existing bookings, user profiles.
- Session table: exactly one row with NULL/default values representing an unauthenticated starting state.

### How it relates to schema.sql

`schema.sql` defines the structure. `world.db` contains the structure plus all data. They must be consistent: every table in `schema.sql` must exist in `world.db` with the same columns and constraints.

A simple consistency check: `sqlite3 new.db < schema.sql` followed by diffing the `.schema` output against `world.db`'s `.schema` output should show no differences.

### Size guideline

A typical `world.db` is 50KB to 500KB. Under 50KB usually means the seed data is too sparse for diverse tasks. Over 500KB usually means reference data is unnecessarily large (e.g., 10,000 products when 100 would suffice).

### Session table initialization

The session table in `world.db` must have exactly one row:

```sql
INSERT INTO session (session_id) VALUES (1);
```

All nullable columns start as NULL. Session state (authentication, user identity, etc.) is set via `world_setup` SQL in `task.json` before the backend is instantiated. This ensures every session starts from a clean, unauthenticated state by default.

### Deterministic IDs in seed data

Pre-seeded records in mutable tables should use IDs that follow the same format the backend's `_next_id` helper will use:

- Credit cards: `CC0000001`, `CC0000002`, ...
- Bookings: `BK0000001`, `BK0000002`, ...
- Insurance policies: `INS0000001`, `INS0000002`, ...

This ensures that new IDs generated during a session (e.g., `CC0000006` after 5 pre-existing cards) do not collide with seed data.
