---
name: tool-surface-design
description: Guide for designing high-quality tools.jsonl definitions following MCP best practices. Covers tool-level description rules, parameter-level description rules, category rules, security patterns, unified return format, and design anti-patterns. Use this skill when creating or reviewing tool definitions to ensure LLM agents can make correct call decisions and fill every parameter without guessing.
---

# Tool Surface Design

This skill provides the complete ruleset for designing tool definitions in `tools.jsonl`. Every rule here exists because it directly affects whether an LLM agent can correctly select and invoke tools. These practices were refined through iterative development of production environments like travel_booking.

The goal: an LLM agent, seeing only the tool schema (no other context), must be able to make the correct call decision and fill every parameter correctly without guessing.

## Tool-Level Description Rules

The `description` field on each tool is the single most important signal for LLM tool selection. Write it as if the LLM has never seen your environment before and will read only this one field to decide whether to call this tool.

### Rule 1: Open with a direct statement of core function

The first sentence must state what the tool does. No filler, no preamble.

Bad: "A helpful tool for managing flight bookings in the travel system."
Bad: "This tool is used to handle the booking process."
Good: "Book a flight between two airports, charging the specified credit card."
Good: "Cancel an existing flight booking and refund the travel cost to the original credit card."

### Rule 2: State when to use

Describe the user intent or situation that should trigger this tool.

Example: "Use when the user wants to book a new flight after confirming the cost and selecting a payment method."

### Rule 3: State when NOT to use (negative constraints)

This is the single most impactful quality signal for preventing LLM hallucination and tool misuse. Negative constraints prevent the LLM from calling the wrong tool in ambiguous situations.

Example: "Do not use this tool to modify an existing booking -- cancellation is permanent and cannot be undone."
Example: "Do not use this tool with IATA codes -- it only accepts full city names."
Example: "Do not use to check login status -- use travel_get_login_status instead."

### Rule 4: Describe the success return structure

State what the tool returns on success, including field names. The LLM needs to know what it will get back to plan subsequent steps.

Example: "On success, returns {booking_id, transaction_id} which can be used with retrieve_invoice."
Example: "Returns a list of {iata_code, city_name} objects sorted alphabetically by IATA code."

### Rule 5: Describe error conditions with error codes

List the specific failure reasons and the error codes the LLM will see.

Example: "Fails with CARD_NOT_FOUND if the card_id does not exist. Fails with INSUFFICIENT_FUNDS if the card balance is too low. Fails with BUDGET_EXCEEDED if the cost exceeds the session budget limit."

### Rule 6: State prerequisite calls

If the tool requires another tool to be called first, say so explicitly. This is critical for multi-step workflows.

Example: "Prerequisite: must call authenticate_travel first to obtain a valid access_token."
Example: "Does not require authentication."

### Rule 7: Short sentences, one constraint per sentence

Long compound sentences with multiple nested clauses are hard for LLMs to parse reliably. Break them up.

Bad: "This tool books a flight and requires an access token that must be obtained by first calling authenticate_travel and also checks the budget limit if one has been set."
Good: "Book a flight between two airports, charging the specified credit card. Requires a valid access_token from authenticate_travel. Checks the session budget limit if one has been set via set_budget_limit."

### Rule 8: No prohibited content

Do not include:
- Implementation details: "uses SQLite internally", "calls the database", "based on Node.js"
- Marketing language: "powerful", "efficient", "convenient", "seamless"
- Subjective adjectives: "easy", "fast", "robust"
- Undefined abbreviations or jargon without explanation
- Non-English text: all descriptions, examples, and error messages must be in English, even if the source skill is in another language

## Parameter-Level Description Rules

Each parameter in `inputSchema.properties` must have a `description` field. The LLM uses this to decide what value to fill in.

### Rule 1: Open with what the parameter represents

The first sentence must make the parameter's purpose unambiguous.

Bad: "The path", "Timeout", "The value"
Good: "The departure airport, specified as a 3-letter IATA code (e.g., 'SFO', 'JFK')."
Good: "The travel budget limit in USD. Must be a positive number."

### Rule 2: State Required/Optional explicitly, with defaults

Even though the `required` array in the schema provides structural information, the description must reinforce it. LLMs weight natural language more than schema structure.

Example: "Required. The credit card ID to charge for this booking."
Example: "Optional. The maximum number of results to return. Defaults to 10 if not specified."

### Rule 3: Exact format constraints

For string parameters, specify the exact format:
- "YYYY-MM-DD" for dates
- "3-letter IATA code" for airports
- "Must start with 'US'" for passport numbers
- "ISO 4217 3-letter currency code" for currencies

For numeric parameters, specify the unit:
- "in USD"
- "in milliseconds"
- "as a percentage (0-100)"

### Rule 4: All enum values listed with meanings

If the parameter accepts a fixed set of values, list every valid value and explain what each one means. The schema must also have a corresponding `"enum"` array.

Bad: "The travel class (enum)"
Bad: "One of the supported travel classes"
Good: "Must be one of: 'economy' (standard seating, 1x base cost), 'business' (premium seating, 2x base cost), 'first' (luxury seating, 5x base cost)."

### Rule 5: Value ranges for numeric parameters

State the valid range, or at minimum clarify boundary behavior.

Example: "Must be a positive number greater than 0."
Example: "Integer between 1 and 100 inclusive."
Example: "Non-negative float. Zero means no budget limit."

### Rule 6: Forbidden/invalid values explicitly stated

State values that are explicitly invalid when there are non-obvious restrictions.

Example: "Must differ from travel_from -- a flight cannot depart and arrive at the same airport."
Example: "Duplicate card_number values are rejected with CARD_ALREADY_REGISTERED."

### Rule 7: Dependencies on other parameters

If this parameter's validity or interpretation depends on another parameter or on prior tool calls, state the dependency.

Example: "Must be a card_id obtained from register_credit_card or get_all_credit_cards."
Example: "Required when insurance_type is 'premium', ignored for 'basic'."

### Rule 8: Concrete examples for non-obvious formats

Provide at least one example for parameters with non-obvious formats.

Example: "e.g., '2024-06-15'"
Example: "e.g., 'SFO', 'JFK', 'LAX'"
Example: "e.g., 'US1234567'"

Not needed for self-explanatory parameters like boolean flags or simple string identifiers.

## Category Rules

There are exactly two valid categories. No others.

### `query`

The tool reads state without modifying it. No side effects. No writes to any database table.

Examples: `get_flight_cost`, `list_all_airports`, `get_booking_history`, `get_credit_card_balance`, `travel_get_login_status`

### `mutation`

The tool modifies state. This includes any operation that creates, updates, or deletes database rows.

Examples: `book_flight`, `cancel_booking`, `register_credit_card`, `authenticate_travel`, `set_budget_limit`

Note: authentication and registration are mutations, not queries. They modify the session table or insert new rows.

### No other categories

Do not use `management`, `admin`, `utility`, or any other category value. Every tool is either a `query` or a `mutation`.

## Security Patterns

### Authentication requirements for sensitive data

Tools that return sensitive data (credit card numbers, CVV/CVC values, passwords, authentication tokens, personal identification numbers) must require an `access_token` parameter and validate it before returning data.

### Masked return values

Return masked versions of sensitive data, not full values.

Good: `"last_four_digits": "4567"` (only the last 4 digits of the card number)
Bad: `"card_number": "4532015112830456"` (full card number exposed)

### Never expose CVV/CVC

CVV/CVC values must never appear in any tool response, even with authentication. They are write-only: accepted as input during card registration but never returned.

### Token validation

Authentication tokens must be validated against the session table using datetime comparison (ISO 8601 strings). Token expiry is time-based (TTL), not call-count-based.

## Unified Return Format

All tools in an environment must use the same top-level return structure. No exceptions.

### Success

```json
{"success": true, "data": <payload>}
```

### Error

```json
{"success": false, "error": {"code": "ERROR_CODE", "message": "Human readable message"}}
```

### Error code conventions

- Use SCREAMING_SNAKE_CASE: `CARD_NOT_FOUND`, `INSUFFICIENT_FUNDS`, `TOKEN_EXPIRED`
- Be specific: `CARD_NOT_FOUND` is better than `NOT_FOUND`
- Be consistent: the same error condition must use the same error code across all tools

### Error message conventions

- Human-readable and actionable
- State what went wrong and, when possible, what to do about it
- Include relevant values: "No credit card found with id 'CC0000099'." rather than "Card not found."

## No-Auth Environments

Not all environments have authentication. File systems, vehicle control, IoT devices, and similar domains have no concept of user login. For these environments:

- Tool descriptions must NOT mention `access_token`, authentication prerequisites, or "call authenticate first".
- Tools may still have prerequisites of other kinds. For example, `startEngine` requires doors locked and brake pressed — describe these as domain-specific preconditions, not auth requirements.
- session state (via `world_setup` SQL) describe initial domain state (fuel level, working directory, device mode), not auth state. The Session Initialization section in ENVIRONMENT.md documents these domain-specific parameters.
- The security section does not apply — there are no sensitive credentials to protect in a file system or vehicle simulation.

## Design Anti-Patterns

These are specific mistakes to avoid. Each was observed in real environment development.

### Anti-pattern 1: Information leakage without authentication

A tool that returns user-specific data (card balances, booking history, personal info) without requiring an access_token. Even if the data seems harmless, unauthenticated access creates unrealistic scenarios.

Fix: add `access_token` as a required parameter and validate it with `_check_access`.

### Anti-pattern 2: Inconsistent return formats

Some tools return `{"success": true, "data": {...}}` while others return `{"result": {...}}` or `{"error": "..."}`. This forces per-tool parsing logic in the trajectory evaluator.

Fix: use the unified return format for every tool without exception.

### Anti-pattern 3: Optional parameters that should be required

A parameter like `booking_id` on `retrieve_invoice` is listed as optional. If it is omitted, the tool cannot function. Making it optional invites the LLM to omit it and fail.

Fix: if a tool cannot function without a parameter, put it in the `required` array.

### Anti-pattern 4: Token expiry by call count

Tracking token validity by counting how many times `call()` has been invoked. This couples authentication to an invisible counter the LLM cannot observe.

Fix: use TTL-based expiry with ISO 8601 timestamp comparison. The token expiry time is stored in the session table and checked against the current time.

### Anti-pattern 5: Returning full sensitive data

A `get_all_credit_cards` tool returns the full card number and CVV. Even with authentication, this is an unnecessary security risk and produces unrealistic training data.

Fix: return `last_four_digits` instead of `card_number`. Never return CVV/CVC in any response.

### Anti-pattern 6: Ambiguous tool boundaries

Two tools with overlapping functionality where the LLM cannot tell which one to use. Example: `search_flights` and `get_flight_cost` both seem to return flight pricing.

Fix: use negative constraints (Rule 3 above) to clearly delineate when to use each tool. Make the first sentence of each description unambiguous about scope.
