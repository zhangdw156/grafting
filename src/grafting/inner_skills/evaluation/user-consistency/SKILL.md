---
name: user-consistency
description: Check each user message for consistency with the user_agent_config — knowledge boundary, personality, natural language, and goal alignment.
---

# User Consistency Check

Verify that every user message is consistent with the
user_agent_config defined in the task. Inconsistent user
behavior produces unrealistic training data.

## Inputs

- `trajectory.json` — the full trajectory with messages
- The `user_agent_config` from the trajectory's embedded task:
  - `role` — who the user is
  - `personality` — behavioral traits
  - `knowledge_boundary` — what the user knows and does not know

## Process

### 1. Parse the User Agent Config

Read and internalize the three fields:
- What role does this user have?
- What personality traits should they exhibit?
- What information do they know vs. not know?

### 2. Check Each User Message

For each message with `role: "user"`, check the following:

#### Knowledge Boundary Violation

Does the user mention information that `knowledge_boundary`
says they do not know?

- VIOLATION: knowledge_boundary says "does not know reservation
  ID" but user says "My reservation is GJLSXX"
- VIOLATION: knowledge_boundary says "does not know payment
  method IDs" but user says "Use credit_card_3786623"
- OK: knowledge_boundary says "knows her name is Emma Kim"
  and user says "My name is Emma Kim"
- OK: User provides information after the assistant retrieved
  and showed it to them (the user now knows it from the
  conversation)

Important: information revealed by the assistant during the
conversation becomes known to the user. Only check against
knowledge_boundary for information the user produces BEFORE
it was revealed by the assistant.

#### Personality Drift

Does the user's tone and behavior match the `personality` field?

- DRIFT: personality says "polite and organized" but user is
  rude or demanding in a message
- DRIFT: personality says "shares details incrementally" but
  user dumps everything in a single message
- DRIFT: personality says "efficient and direct" but user is
  overly verbose and tangential
- OK: Slight variation in tone across messages (humans are
  not perfectly consistent)
- OK: A user described as "methodical" or "gives clear
  instructions upfront" who provides information across two
  or three messages (e.g., login first, then task details)
  — this is still methodical, structured conversation
- OK: A user described as "provides all details upfront" who
  gives all details at the point of each request, even if
  spread across multiple conversation turns for different
  sub-tasks

Use reasonable judgment — minor variations are acceptable.
Flag only clear, sustained departures from the described
personality. A single message that is slightly less formal
or slightly more verbose than expected is NOT drift.

#### AI-Like Language

Does the user message sound like it was written by a human
or by an AI?

Indicators of AI-generated language:
- Numbered or bulleted lists in conversational context
  (real users rarely structure requests as "1) ... 2) ... 3) ...")
- Overly formal or structured phrasing
- Redundant politeness patterns ("Thank you so much for your
  help with this matter, I really appreciate your assistance")
- Technical jargon or precise terminology a real user in the
  described role would not use
- Perfect grammar and punctuation in a casual context
- Meta-commentary about the conversation itself

Indicators of natural human language:
- Casual, conversational tone
- Incomplete sentences or fragments
- Natural filler ("um", "so", "yeah")
- Slight imprecision in descriptions
- Emotional undertones matching the situation

#### Goal Drift

Does the user request something not present in the demand?

- DRIFT: Demand is about changing a flight but user also asks
  to cancel their insurance (not in the demand)
- DRIFT: Demand is about updating a DOB but user starts
  asking about loyalty program benefits
- OK: User asks clarifying questions related to the demand
- OK: User responds to assistant's questions about the demand

### 3. Report

For each issue found, record:

```
Message index 0 (user):
  Content: "Hi, my reservation GJLSXX needs updating..."
  Issue: knowledge_boundary says "does not know reservation ID"
         but user directly provides "GJLSXX"
  Type: knowledge_boundary_violation

Message index 4 (user):
  Content: "Please proceed with the following changes:
            1) Update DOB to 1968-08-13
            2) Change flight to May 16 business
            3) Add 2 checked bags to the reservation"
  Issue: Numbered list format is AI-like; a real user would
         describe this conversationally
  Type: ai_language
```

If NO issues found, set `user_consistent = true`.
If ANY issue found, set `user_consistent = false`
and record each as an issue.

## Judgment Guidelines

- Be strict on knowledge_boundary violations — these are
  objective and easy to verify.
- Be moderate on personality drift — allow natural variation,
  flag only clear mismatches.
- Be moderate on AI-like language — some users are naturally
  structured; flag only clearly robotic patterns.
- Be strict on goal drift — the user should stay within the
  scope of the demand.
