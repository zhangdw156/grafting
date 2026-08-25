---
name: synthesis-registry
description: Manage the environment synthesis lifecycle — skill index, environment dedup, registry, and consumed tracking. This skill provides all scripts the synthesis agent needs for searching related skills, checking duplicates, registering results, and marking consumed skills.
---

# Synthesis Registry

This skill provides the scripts and infrastructure for the synthesis lifecycle:

1. **Skill index** — FTS5 full-text search over 28,768 raw skills
2. **Environment index** — FTS5 over synthesized environments for dedup
3. **Registry** — tracks processed skills, synthesized environments, rejections
4. **Consumed tracking** — marks used skills to prevent re-processing

## Sandbox Layout

All database files are symlinked into the sandbox project root:

```
project/                          ← your working directory (cwd)
  raw_skills/                     → symlink to raw skills
  skill_index.db                  → symlink to FTS5 skill index
  registry.db                     → symlink to registry
  environment_index.db            → symlink to environment index
  output/                         ← write results here
```

## Scripts

All scripts are in this skill's `scripts/` directory.

### Pre-synthesis setup (one-time)

| Script | Purpose |
|--------|---------|
| `python scripts/build_skill_index.py <skills_root> <output_db>` | Build FTS5 index over all raw skills |

### During viability assessment

| Script | Purpose |
|--------|---------|
| `python scripts/check_processed.py registry.db --skill-id X` | Skip already-processed skills (exit 0 = processed) |
| `python scripts/search_environments.py . check-dup --skill-description "..."` | Check if new skill would duplicate existing environment |
| `python scripts/search_skills.py skill_index.db --query "..." [--same-repo-as X]` | Find related skills when tool count is insufficient |

### After synthesis

| Script | Purpose |
|--------|---------|
| `python scripts/register_env.py registry.db --env-id X --source-skills X,Y --env-root output/<env_name>` | Register synthesized environment |
| `python scripts/search_skills.py skill_index.db --consume X,Y --by <env_id>` | Mark consumed skills |
| `python scripts/search_environments.py . build` | Rebuild environment index after adding new environment |

### After rejection

| Script | Purpose |
|--------|---------|
| `python scripts/register_rejection.py registry.db --skill-id X --reason "..."` | Record rejection |

### Analytics

| Script | Purpose |
|--------|---------|
| `python scripts/query_envs.py registry.db` | List all synthesized environments |
| `python scripts/stats.py registry.db` | Summary statistics |

## Synthesis Flow

```
1. check_processed.py          → skip if already done
2. search_environments.py      → skip if would duplicate
3. [viability assessment]      → tools enough?
4. search_skills.py            → find related skills if not enough
5. [synthesis]                 → produce environment
6. register_env.py             → record success
7. search_skills.py --consume  → mark skills as used
8. search_environments.py build → update env index
```

## Database Files

All database files are symlinked into the sandbox project root:

| File | Purpose |
|------|---------|
| `skill_index.db` | FTS5 index over raw skills (built once, consumed flag updated) |
| `environment_index.db` | FTS5 index over synthesized environments (rebuilt after each synthesis) |
| `registry.db` | Processed skills + environments + rejections |
