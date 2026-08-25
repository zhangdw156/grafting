---
name: skill-viability-assessment
description: Assess whether a raw skill (or skill cluster) is viable for environment synthesis. Use this as the first step before any synthesis work. Reads the raw skill's SKILL.md, scripts/, _meta.json and all other files, then evaluates mockability, data constructability, and tool count potential (extract + expand). Outputs pass/reject with detailed reasoning.
---

# Skill Viability Assessment

You are the entry gate for environment synthesis. Given a raw skill directory (from grafting-skills-scripts), determine whether it is worth synthesizing into an executable environment.

## Sandbox Layout

This skill runs inside a sandbox with the following structure:

```
project/                          ← your working directory (cwd)
  raw_skills/                     → symlink to raw skills
  skill_index.db                  → symlink to FTS5 skill index
  registry.db                     → symlink to registry
  environment_index.db            → symlink to environment index
  output/                         ← write results here
```

## Input

A raw skill directory at `raw_skills/<skill_id>/`. The directory typically contains:
- `SKILL.md` — domain description, purpose, usage patterns, implementation guidance
- `scripts/` — executable scripts (Python, Node.js, shell) that implement the skill's functionality
- `_meta.json` — metadata: `skill_name`, `repo`, `owner`, `repo_url`
- Possibly other files: config templates, reference docs, examples

Read ALL files. The scripts/ directory is the most important — it contains the actual operations that will become tools.

## Assessment Criteria

Evaluate these five criteria in order. If any criterion is a hard fail, you can stop early.

### 1. Tool Count Potential (two-phase estimation)

This is the most important criterion. Estimate how many meaningful tools the environment would have.

**Phase 1 — Extract from scripts/**

Read each script file. Identify distinct operations by looking for:
- HTTP API calls (each endpoint = one tool candidate)
- CLI commands with distinct subcommands
- Functions that perform CRUD operations
- Data processing pipelines with distinct steps

Count conservatively. A script with 3 API calls to the same endpoint with different parameters is 1 tool, not 3.

**Phase 2 — Domain-driven expansion**

Based on the domain, identify tools that are natural and necessary but not present in scripts/:
- CRUD completions: if there's `create_issue`, the environment should also have `get_issue`, `update_issue`, `delete_issue`, `list_issues`, `search_issues`
- Management tools: authentication, session management, configuration
- Query/discovery tools: list_all, get_by_id, search, filter
- Status/state tools: get_status, get_history, get_statistics
- Relationship tools: assign, link, move, transfer

Only count expansions that are meaningful for the domain. Do not pad with artificial tools.

**Decision:**
- Phase 1 + Phase 2 ≥ 12 → PASS
- Phase 1 + Phase 2 < 12 → REJECT (reason: insufficient tool surface)

### 2. Mockability

Can every external dependency be simulated with deterministic SQLite-backed code?

**PASS indicators:**
- REST API calls with CRUD semantics (most common — easy to mock with SQL tables)
- Database queries (already SQL-native)
- File system operations (inode table pattern)
- State machine transitions (status column + validation logic)

**FAIL indicators:**
- Requires real-time data that cannot be pre-seeded (live stock prices with sub-second updates)
- Requires GPU/ML inference (image generation, LLM calls)
- Requires network connectivity for core functionality (not just auth)
- Requires hardware interaction (Bluetooth, USB, camera)

**CONDITIONAL indicators** (pass with caveats):
- OAuth2 flows → mock with deterministic token generation
- Webhook receivers → mock with simulated event queue
- Rate limiting → mock with counter in session table
- Pagination → mock with LIMIT/OFFSET

**Decision:**
- All dependencies mockable → PASS
- Core functionality requires unmockable dependency → REJECT
- Conditional cases → PASS with noted caveats

### 3. Data Constructability

Can you design a reasonable SQLite schema and populate it with meaningful seed data?

**PASS indicators:**
- Clear entities with relationships (users, projects, issues, orders)
- Enumerable states and types (status workflows, categories)
- Data that can be invented realistically (names, dates, amounts, descriptions)

**FAIL indicators:**
- Domain requires proprietary/licensed data (specific medical records, legal precedents in a specific jurisdiction that must be exact)
- Data is inherently real-time and cannot be snapshot (live sensor readings)
- Data requires domain expertise to construct correctly (valid chemical formulas, correct legal citations)

**Decision:**
- Schema designable + seed data constructable → PASS
- Cannot design meaningful schema → REJECT

## Hard Rejection Criteria

Reject the skill IMMEDIATELY if any of these apply. Do not attempt synthesis.

### R1. External binary dependency
The skill requires system binaries to function (latexmk, pdflatex, ffmpeg, imagemagick, docker, node, etc.). These cannot be packaged into a SQLite-backed environment.

Examples: LaTeX compilation skills, video processing skills, Docker orchestration skills.

### R2. Live API authentication required
The skill's core functionality requires valid API credentials (API keys, OAuth tokens, bot tokens) to operate. If every tool call would fail without credentials, the skill cannot be mocked.

Examples: Telegram bots requiring BOT_TOKEN, skills requiring STRIPE_API_KEY for every operation.

Exception: Skills where auth is a session management layer (like travel_booking's authenticate_travel) ARE viable — the auth itself is mocked.

### R3. Real-time external data dependency
The skill's value comes from live, real-time data that cannot be meaningfully pre-seeded (live stock tickers with sub-second updates, real-time weather, live sensor feeds).

Exception: Historical or reference data CAN be pre-seeded (past stock prices, airport codes, exchange rates).

### R4. Mixed language runtime
The skill requires multiple language runtimes to execute (Node.js + Python, Ruby + Shell). The environment backend must be pure Python with stdlib only.

Exception: Shell scripts that are just CLI wrappers for a single API are fine — extract the API calls, don't port the shell.

### R5. Event-driven / async-first architecture
The skill is designed as an event loop (Telegram bot handlers, webhook receivers, WebSocket servers). The synchronous EnvironmentBackend.call() interface cannot model event-driven systems.

### R6. File I/O as core state
The skill's primary state is in files on disk (LaTeX documents, config files, log files), not in structured data. Environment backends use SQLite for state, not filesystem.

Exception: File management environments (like gorilla_file_system) ARE viable because they model a filesystem IN SQLite.

### R7. No executable scripts
The skill has no scripts/ directory, or scripts/ contains only documentation/README files with no executable code. Nothing to extract tools from.

### 4. Deduplication Check

Before passing, check if the registry already has a very similar environment.

- Run `python scripts/check_processed.py registry.db --skill-id <skill_id>` (from the `synthesis-registry` skill) to skip already-processed skills
- Run `python scripts/search_environments.py . check-dup --skill-description "..."` (from the `synthesis-registry` skill) to find similar existing environments
- If a similar environment exists with 80%+ tool overlap, recommend MERGE instead of new synthesis

### 5. Searching for Related Skills

When the raw skill alone cannot reach 12 tools, search for related skills to combine:

1. Search by keywords from the skill description:
   ```
   python scripts/search_skills.py skill_index.db --query "<keywords>"
   ```

2. Search same repo:
   ```
   python scripts/search_skills.py skill_index.db --same-repo-as <skill_id>
   ```

3. Review results -- read each candidate's SKILL.md to check:
   - Does it target the same API or complement this skill?
   - Would combining add meaningful tools (not duplicates)?

4. If combined total >= 12: incorporate those skills into the synthesis.
   Read them from `raw_skills/<found_skill_id>/` and include their operations.

5. If still < 12 after searching: REJECT.

## Output Sequence

You MUST write the viability assessment FIRST, before any environment files.

1. Write `output/VIABILITY.json` with the assessment result
2. If verdict is "reject": write `output/REJECTED.json` and STOP
3. If verdict is "pass": proceed with synthesis (steps 4-11)

Never skip the viability assessment. Never write environment files before VIABILITY.json.

## Output Format

```json
{
  "skill_id": "00001_jira-agile",
  "skill_name": "jira-agile",
  "repo": "agent-jira-skills",
  "verdict": "pass",
  "additional_skill_ids": [],
  "phase1_tools": 8,
  "phase2_tools": 14,
  "total_estimated_tools": 22,
  "mockable": true,
  "mockability_caveats": [],
  "data_constructable": true,
  "mockable": true,
  "dedup_status": "no_conflict",
  "similar_environments": [],
  "reasoning": "8 tools directly extractable from scripts (list boards, get sprints, create sprint, etc.). Expands naturally to 22 with CRUD completion and board configuration tools. All REST API calls mockable with SQLite. Clear entity model (boards, sprints, issues).",
  "suggested_group": "agent-jira-skills cluster (8 related skills)",
  "risks": []
}
```

For rejections:
```json
{
  "skill_id": "00009_edgeone-pages-deploy",
  "verdict": "reject",
  "phase1_tools": 1,
  "phase2_tools": 3,
  "total_estimated_tools": 4,
  "reasoning": "Only 1 operation in scripts/ (POST HTML to CDN). Domain is stateless deployment with no management API. Even with expansion (list deployments, delete, get status), only reaches 4 tools. Below 12-tool threshold.",
  "rejection_criteria": "insufficient_tool_surface"
}
```

## Important

- Read the actual script code, not just file names. A script named `bulk-create.py` might contain 5 distinct API calls.
- Be conservative in Phase 1 (only count what's clearly there) and generous in Phase 2 (include all reasonable expansions).
- A skill with 3 extracted tools but rich expansion potential (e.g., a CRM with only "create contact" script but obvious need for full CRUD + search + import/export) is still viable.
- When in doubt about expansion, ask: "Would a real API in this domain have this tool?" If yes, count it.
