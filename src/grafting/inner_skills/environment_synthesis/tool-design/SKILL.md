---
name: tool-design
description: Design the complete tool surface for an environment. Combines tool extraction (reading raw skill scripts to identify operations), domain-driven expansion (CRUD completion, auth, relationships), and tools.jsonl authoring (MCP-quality descriptions, enum constraints, security patterns, unified return format). Use after skill-viability-assessment passes. Produces the final tools.jsonl file.
---

# Tool Design

Given a raw skill that passed viability assessment, design and write the complete `tools.jsonl` for the environment. This is a three-step process:

1. **Extract** — identify tool candidates from scripts/
2. **Expand** — add domain-appropriate tools not present in scripts/
3. **Author** — write each tool's schema with MCP-quality descriptions

## Sandbox Paths

- Read raw skill from: `raw_skills/<skill_id>/`
- Write output to: `output/<env_name>/spec/tools.jsonl`

## Step 1: Extract Tool Candidates

Read every file in the raw skill's `scripts/` directory. Identify distinct operations.

Look for:
- HTTP API calls: each distinct endpoint + method = one tool candidate
- CLI subcommands: each subcommand = one tool candidate
- Functions with external effects: each distinct operation = one tool candidate

Do NOT count as separate tools:
- Internal helpers (auth refresh, error formatting, logging)
- Same endpoint with different parameters (one search tool, not many)
- Setup/teardown code

Also read SKILL.md's implementation sections — they often describe operations not yet in scripts/.

## Step 2: Domain-Driven Expansion

Based on the domain and Step 1 tools, add missing tools that a real API would have.

**Expansion patterns:**
- CRUD completion: `create_X` → add `get_X`, `update_X`, `delete_X`, `list_X`, `search_X`
- Auth/session: if tools need auth → add `authenticate`, `get_login_status`, `get_session_state`
- Relationships: if entities relate → add `assign`, `move`, `link`, `add_label`
- Aggregation: if multiple entities → add `get_statistics`, `get_activity_log`
- State transitions: if status fields → add `transition`, `get_available_transitions`

**Constraints:**
- Every expanded tool must be implementable with SQLite
- Every expanded tool must make domain sense
- Don't pad to hit a number — each tool must enable a meaningful task step
- CRUD completeness: for every entity the environment manages, ensure create, get, list, update, and delete tools exist unless the entity is explicitly immutable. Missing CRUD operations limit task diversity

## Step 3: Author tools.jsonl

For each tool (extracted + expanded), write one JSONL line with fields: `name`, `description`, `category`, `inputSchema`.

Refer to `references/surface_design_rules.md` for the complete description quality rules and `references/contract.md` for the environment contract.

### Quick reference (key rules)

**Tool-level description must:**
- Open with direct statement of core function
- State when to use and when NOT to use
- Describe success return structure with field names
- List error codes and failure conditions
- State prerequisite calls
- Short sentences, one constraint per sentence

**Parameter-level description must:**
- State purpose, Required/Optional with defaults
- Exact format constraints + enum values with meanings
- Value ranges, forbidden values, dependencies
- Every non-trivial parameter MUST have at least one concrete example (e.g., `"2024-06-15"`, `"SFO"`, `"CC0000001"`). Only omit examples for obvious boolean or simple string parameters
- Every parameter with a specific format MUST state the format explicitly: date formats (`YYYY-MM-DD`), ID formats (prefix + digits, e.g., `BK0000001`), enum values
- Schema must have `"enum"` array for fixed-value params

**Categories:** only `query` (reads) and `mutation` (modifies state)

**Return format:** all tools use `{"success": true, "data": ...}` / `{"success": false, "error": {"code", "message"}}`

**Security:** auth-required tools mask sensitive data, never expose CVV

## Conversational Accessibility

Tools are used in a user-assistant conversation, not by a programmatic API client. A user says "cancel my flight to Tokyo" — they don't know `booking_id: BK0000042`. Design for this:

- **ID discovery path:** For every mutation tool that takes an internal ID (e.g., `booking_id`, `order_id`), there must be a query tool that finds that ID from natural information the user would know (name, email, date, description, destination).
- **Search tools:** Add `search_*` or `list_*` tools that accept partial/fuzzy inputs (e.g., search by destination, date range, customer name) and return matching records with their IDs. A user should never need to guess or memorize an internal ID.
- **Auth via natural credentials:** Authentication must accept things a user would say: username, email, password. Never require OAuth client secrets, API keys, or bearer tokens as user-facing inputs.
- **No dead session state:** Every field written to the sessions table must be readable by at least one query tool. If the backend writes `last_active` or `preferences` to the session, a tool must expose it.

## Output

Write `output/<env_name>/spec/tools.jsonl` — one tool per line, JSONL format.
