---
name: task-demand-clarity-check
description: Verify that each demand has a clear, unambiguous completion condition and that demands do not overlap. Prevents user agent confusion where it cannot distinguish done from incomplete.
---

# Task Demand Clarity Check

Verify that each demand is clear enough for the simulation's intent
recognition to judge completion accurately.

## Why This Matters

If a demand says "handle the flight stuff", intent recognition
cannot tell when the assistant has finished — is it done after
checking the flight? After changing it? After adding bags too?
Vague demands cause the user agent to repeat already-completed
requests or prematurely mark demands as done.

## Checks

### 1. Completion Condition is Unambiguous

Each demand should describe a single, specific action with a clear
end state:

- ✓ "Change the flight from May 17 to May 16"
  → Done when flight date is updated
- ✓ "Add 2 extra checked bags to the reservation"
  → Done when bags are added
- ✗ "Handle the travel arrangements"
  → What counts as "handled"?
- ✗ "Fix everything with the booking"
  → Undefined scope

### 2. No Overlap Between Demands

Two demands must not describe the same action in different words:

- ✗ Demand 1: "Update the passenger name on the booking"
  Demand 2: "Correct the traveler's last name on the reservation"
  → Same action, different wording

- ✓ Demand 1: "Update the passenger's last name"
  Demand 2: "Add two checked bags"
  → Distinct actions

### 3. Each Demand Targets a Distinct Mutation

Since each demand comes from a distinct subgraph with a different
mutation anchor, demands should naturally target different tools.
Verify this holds — two demands should not end up exercising the
same mutation tool.

Exception: for `long_context` rule, multiple demands MAY target
the same mutation tool with different parameters (e.g., "book
flight A" and "book flight B").

### 4. Demand Text is Specific Enough

The demand should contain enough context for the user agent to
know what to say and for intent recognition to judge the response:

- ✗ "Do something with the account"
- ✓ "Add a new customer named Alex Johnson in Boston"
- ✓ "Move app.log, error.log, and access.log into logs/archive"

### 5. Demand Describes Goals, Not Implementation Steps

The demand must describe **what** the user wants to achieve, not
**how** to achieve it. It should not prescribe specific tool calls,
API sequences, or intermediate steps. The assistant should be free
to choose the most efficient path.

- ✗ "List the available repositories to find beacon-web, set it as
  the active repository, then search for files related to 'client'"
  → Prescribes tool-level steps
- ✓ "Investigate the HTTP client in the beacon-web repository and
  create a dependency report"
  → States the goal, leaves the path to the assistant

- ✗ "Check which tokens are supported, then create a basket with
  50% RNDR, 30% PYTH, 20% ONDO"
  → First part prescribes a discovery step
- ✓ "Create a basket called 'AI & Oracle Blend' with 50% RNDR,
  30% PYTH, 20% ONDO"
  → States the desired outcome

If a demand contains procedural steps, rewrite it to focus on the
desired outcome. The assistant may need to discover information or
take intermediate steps, but that should emerge naturally from the
conversation, not be dictated by the demand.

### 6. knowledge_boundary Does Not Leak Demand Goals

Check that `user_agent_config.knowledge_boundary` does NOT contain
any demand goals rephrased as user knowledge:

- ✗ "Knows they want to cancel the reservation" — this is demand
  content
- ✗ "Expects a $100 travel certificate" — this is demand content
- ✓ "Knows their name" — this is background knowledge

### 7. knowledge_boundary ID Plausibility

Check that any IDs in `knowledge_boundary` are ones a real person
in the described role would naturally have. Ask: "Would this user
have received this identifier from a prior interaction (email,
receipt, notification)?"

- ✓ Booking confirmation code from an email
- ✓ Support ticket number from a notification
- ✓ File paths the user works with
- ✗ Database-internal user_id like `emma_kim_4489`
- ✗ Payment method ID like `credit_card_3786623`

Both "user knows the ID" and "user doesn't know the ID" are
valid — they just produce different trajectory shapes. But the
ID must be plausible for the persona.

If knowledge_boundary contains demand goals, the user agent will
skip ahead to future demands. Remove the demand-specific content
and keep only factual background knowledge.

### 8. Report

```
Demand 1: "Change flight from May 17 to May 16"
  Completion: clear (flight date changed) ✓
  Overlap: none ✓
  Mutation: update_reservation_flights ✓

Demand 2: "Add 2 extra checked bags"
  Completion: clear (bags added) ✓
  Overlap: none ✓
  Mutation: update_reservation_baggages ✓

All demands pass clarity check.
```

If any demand fails, rewrite it to be more specific before
proceeding.
