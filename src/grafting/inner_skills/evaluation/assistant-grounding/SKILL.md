---
name: assistant-grounding
description: Check each assistant message for hallucinations — text claims, operation claims, and function_call arguments must all be traceable to tool results or user-provided information.
---

# Assistant Grounding Check

Verify that every claim the assistant makes is grounded in
the conversation history. Ungrounded claims are hallucinations.

## Inputs

- `trajectory.json` — the full trajectory with messages
- `output/replay_result.json` — replay results for reference

## Process

### 1. Build the Conversation Context

Process messages in order. At each assistant message, your
"available context" is everything that appeared before it:
- All preceding user messages (user-provided information)
- All preceding tool results (function role messages)
- Tool schema descriptions and examples in `env/spec/tools.jsonl`
- The assistant's own prior messages, but only when their claims were
  themselves grounded in earlier evidence

Before flagging a claim, search the ENTIRE preceding conversation for
supporting evidence. Do not limit the check to the nearest tool result.
Pay special attention to:
- Earlier unfiltered list/get-all calls that established exhaustive knowledge
- Information the user supplied in any prior message
- Multiple earlier tool results that jointly support the claim
- Relevant tool schema descriptions, examples, and operation scope

### 2. Check Each Assistant Text Message

For each assistant message that contains text content,
examine every specific claim:

**Data claims**: Numbers, names, IDs, prices, dates, statuses.
Each must be grounded in a preceding user message, tool result, tool schema,
or valid derivation from those sources.

- OK: "Your reservation ID is GJLSXX" — if get_reservation_details
  returned this ID in an earlier turn
- HALLUCINATION: "Your flight costs $200" — if no tool returned
  this price

**Action claims**: Statements like "I've updated...",
"Done, the change is...", "I cancelled..."
Each must correspond to a tool call that succeeded.

- OK: "Done — Raj Brown's DOB has been updated to 1968-08-13" —
  if update_reservation_passengers was called and returned success
- HALLUCINATION: "I've cancelled your reservation" — if
  cancel_reservation was never called

### 3. Check Each Function Call's Arguments

For each `function_calls` entry in an assistant message, check
every argument value:

**Traceable sources** (any of these is acceptable):
- A preceding tool result contains this value
- The user provided this value in a message
- The tool schema description or examples contain this information
- The value is a reasonable derivation (e.g., combining
  first_name + last_name from a user message)
- Multiple preceding sources jointly support the value or conclusion

The assistant's own earlier unsupported claim is not an independent source.

**Hallucinated arguments**: The value appears in neither tool
results nor user messages up to that point.

Example:
```json
{
  "name": "update_reservation_flights",
  "arguments": {
    "reservation_id": "GJLSXX",
    "cabin": "business",
    "flights": [{"flight_number": "HAT015", "date": "2024-05-16"}],
    "payment_id": "credit_card_3786623"
  }
}
```

Check each argument:
- `reservation_id: "GJLSXX"` — was this returned by
  get_user_details or get_reservation_details? ✓
- `cabin: "business"` — did the user request business class? ✓
- `flight_number: "HAT015"` — was this returned by
  search_direct_flight? ✓
- `payment_id: "credit_card_3786623"` — was this returned
  by get_user_details in the payment_methods? ✓

### 4. Report

For each hallucination found, record an issue:

```
Message index 15 (assistant text):
  Claim: "The baggage fee is $75 per bag"
  Source: No tool result contains this price
  Type: text_hallucination

Message index 8 (function_call arguments):
  Argument: payment_id = "credit_card_9999999"
  Source: Not found in any preceding tool result or user message
  Type: parameter_hallucination
```

If NO hallucinations found, set `assistant_grounded = true`.
If ANY hallucination found, set `assistant_grounded = false`
and record each as an issue.

## Important Notes

- Empty assistant messages (content = "") followed by function
  calls are normal — the assistant is making tool calls without
  narration. These are not hallucinations.
- General knowledge claims (e.g., "Business class typically
  includes more legroom") are acceptable — only environment-
  specific data claims need grounding.
- Computed values are acceptable if the computation is correct
  (e.g., "The total is $322" from 161 * 2).

## What is NOT a Hallucination

- **Required parameter defaults**: When a tool's schema requires a
  parameter that the user did not specify, the assistant must supply
  a value. Choosing a reasonable default (e.g., `severity: "high"`,
  `initial_state: "idle"`) is NOT hallucination — it is necessary
  tool usage. This is especially acceptable when the assistant
  discloses the choice to the user.

- **Tool schema enum values**: Using an enum value listed in the
  tool's input schema (e.g., selecting `verification` from an enum
  of `[verification, consensus, majority_vote]`) is grounded in the
  schema itself, even if no prior tool *result* returned that value.

- **Tool schema descriptions and examples**: Information explicitly stated
  or demonstrated in a tool definition is part of the assistant's available
  context. A claim grounded in schema semantics or examples is not a
  hallucination merely because no tool result repeated it.

- **Semantic paraphrasing**: Restating a tool result in natural
  language is acceptable as long as the meaning is preserved. For
  example, saying "the custody chain is intact" when the tool
  returned `status: "verified"`, or inferring "UTC" from a
  timezone-less ISO timestamp, is reasonable interpretation — not
  data fabrication. Only flag paraphrasing when the assistant
  introduces claims that cannot be derived from any tool result.

- **Abbreviation expansion**: Expanding an abbreviation when the meaning is
  unambiguous in context (for example, `OPS` to `Operations`) is semantic
  paraphrasing, not fabrication. Do not allow an expansion that introduces
  a new environment-specific entity or meaning unsupported by context.

- **Narration of planned actions**: Saying "I will do X and Y"
  but executing them sequentially is imprecise narration, not
  hallucination. Only flag if the assistant *claims to have done*
  something it did not do.

- **Logical consequences of operations**: Stating something that is
  a necessary logical consequence of a just-completed operation is
  grounded reasoning, not hallucination. Examples:
  - "The document is currently empty" after creating a new document
    (creation does not populate content)
  - "You are the owner" after creating a resource (creators own
    their resources by default, per tool description semantics)
  - Inferring the current date from a session token's expiration
    timestamp
  - Interpreting an error response within the scope of the request that
    triggered it. For example, if `create_project(team_id="X", name="Y")`
    returns `NAME_CONFLICT`, inferring that the conflict applies within
    team X is a contextual interpretation of the scoped operation.

- **Multi-source logical synthesis**: Combining facts from multiple preceding
  user messages, tool results, or schema descriptions into a conclusion is
  reasoning, not hallucination, even when no single source states the
  conclusion verbatim. Only allow conclusions that follow from the evidence.
  Fabricated identifiers, unsupported counts or percentages, incorrect time
  calculations, and invented causal explanations remain hallucinations.

- **Compositional text in creative parameters**: Some tool parameters
  are creative text fields where the assistant is expected to compose
  content (e.g., issue body, comment body, context_summary, notes).
  When the assistant enriches the user's stated intent with reasonable
  elaboration in these fields, this is helpful composition — not
  parameter_hallucination. Only flag creative parameters when the
  assistant introduces factual claims that contradict the user's
  intent or fabricates specific data (IDs, numbers, dates) that
  should come from tool results.
  Contrast with lookup parameters (IDs, tokens, enum values) where
  the value MUST be traceable to a tool result or user message.

- **User-requested domain knowledge output**: When the user explicitly
  asks the assistant to produce content using its own knowledge
  (e.g., "draft the YAML for me", "format this as APA", "write a
  summary"), the assistant applying domain knowledge to fulfill that
  request is authorized generation — not hallucination. The
  assistant should still use data from tool results where available,
  but structural/formatting knowledge from its training is expected.

- **Domain knowledge versus environment data**: General domain conventions
  are acceptable when presented as general knowledge. Specific facts about
  this environment still require grounding. For example, describing the
  usual Kubernetes DNS naming convention is general knowledge; asserting a
  particular DNS name exists in this cluster requires environment evidence.

## What IS a Hallucination (common patterns)

The specific rules in this section take precedence over broad exemptions
above. Do not excuse a fabricated environment-specific identifier, number,
percentage, timestamp, status, or causal explanation merely by calling it
paraphrasing, domain knowledge, or logical synthesis.

### Search Completeness Assessment

Before flagging a universal claim based on search or list results, consider:

1. **Field coverage**: Does the tool search the relevant structured fields,
   or only a limited free-text field?
2. **Query specificity**: Is the query a rare, specific identifier or a broad,
   ambiguous term?
3. **Prior exhaustive calls**: Did an earlier unfiltered list/get-all call
   already establish the complete result set?
4. **Hedging language**: Did the assistant qualify the claim with language
   such as "based on my search" or "that I can find"?

- **Ungrounded universal claims from partial search results**: Flag the claim
  only when the available search demonstrably cannot establish completeness
  and the assistant makes an unqualified universal statement such as "X is
  the only one" or "there are no Y." Do not flag solely because the final
  supporting call used text matching; account for the full prior context and
  the tool's documented field coverage.

- **Fabricated causal explanations**: When a tool returns a count
  or status without explanation, the assistant must NOT invent
  reasons. For example, if a generate function returns 3 items
  instead of the requested maximum of 4, saying "the metrics
  didn't warrant a fourth" is a hallucination — the tool did not
  explain why.

- **Placeholder credentials in parallel calls**: Using fabricated
  values like `"token-placeholder"` or `"pending"` for required
  authentication parameters when the real value has not yet been
  obtained is a parameter_hallucination, even if the call fails
  and the assistant retries correctly.

## Final Verification Checklist

Before recording a hallucination issue:

1. Identify the exact claim or argument being evaluated.
2. Search the entire preceding conversation, not only adjacent messages.
3. Inspect relevant tool schema descriptions and examples.
4. Check whether the claim is a valid paraphrase, abbreviation expansion,
   computation, contextual error interpretation, or multi-source synthesis.
5. For universal search claims, apply all four completeness factors above.
6. Confirm the claim does not merely state general domain knowledge.
7. Apply the precedence rule: specific unsupported environment facts still
   count as hallucinations.
8. In the issue detail, state which sources were checked and what support is
   missing or contradicted.
