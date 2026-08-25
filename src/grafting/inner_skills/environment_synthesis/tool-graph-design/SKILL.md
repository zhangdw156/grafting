---
name: tool-graph-design
description: Construct tool_graph.json for a packaged environment. Use this skill after tools.jsonl and schema.sql are finalized. The tool graph captures data dependencies and state preconditions between tools, enabling downstream task generation to produce diverse multi-step workflows. Reads tools.jsonl for tool names/categories and schema.sql for table names, then outputs a graph with nodes (one per tool) and edges (data_dependency or state_precondition).
---

# Tool Graph Design

You are a tool graph architect. Your job is to read `tools.jsonl` and `schema.sql` for an environment, then produce a `tool_graph.json` file that captures how tools relate to each other through data flow and state prerequisites.

## Sandbox Paths

- Read tools from: `output/<env_name>/spec/tools.jsonl`
- Read schema from: `output/<env_name>/world/schema.sql`
- Write output to: `output/<env_name>/spec/tool_graph.json`

## Input

Read these files from `output/<env_name>/`:

- `spec/tools.jsonl`: one JSON object per line, each with `name`, `description`, `category`, `inputSchema`
- `world/schema.sql`: the SQL schema defining all tables

## Output

Write `output/<env_name>/spec/tool_graph.json` — a JSON object with two top-level keys: `nodes` and `edges`.

## Nodes

One node per tool. Every tool in `tools.jsonl` must have exactly one node.

```json
{
  "name": "book_flight",
  "category": "mutation",
  "reads": ["session", "credit_cards", "flight_routes", "airports"],
  "writes": ["bookings", "credit_cards"]
}
```

Field rules:

- `name` (string): must exactly match a tool name in `tools.jsonl`. No extras, no missing.
- `category` (string): must exactly match the category in `tools.jsonl`. Either `"query"` or `"mutation"`.
- `reads` (array of strings): list of table names from `schema.sql` that this tool reads during execution. Determine this by analyzing what data the tool needs to fulfill its function.
- `writes` (array of strings): list of table names from `schema.sql` that this tool modifies (INSERT, UPDATE, DELETE). A query tool must have an empty `writes` array. A mutation tool must have a non-empty `writes` array.

### How to determine reads and writes

1. Read the tool's description and parameters from `tools.jsonl`.
2. For each parameter that references an entity (e.g., `booking_id`, `card_id`), the tool reads the table storing that entity.
3. If the tool description says it "creates", "updates", "deletes", or "modifies" something, it writes to the corresponding table.
4. If the tool description says it "checks", "retrieves", "lists", or "gets" something, it reads the corresponding table.
5. Include the `session` table in reads if the tool requires authentication (checks an access token).

## Edges

Edges capture relationships between tools. Each edge is a JSON object:

```json
{
  "from": "register_credit_card",
  "to": "book_flight",
  "type": "data_dependency",
  "via": "card_id"
}
```

Field rules:

- `from` (string): the tool that produces the data or state. Must be a valid node name.
- `to` (string): the tool that consumes the data or requires the state. Must be a valid node name.
- `type` (string): one of two values:
  - `"data_dependency"`: `from` produces a value (ID, result, list) that `to` needs as input. This is the most common edge type. Example: `register_credit_card` returns a `card_id` that `book_flight` needs.
  - `"state_precondition"`: `from` must succeed first to put the system in a state where `to` can operate. Example: `authenticate_travel` must run before `book_flight` because a valid token is required.
- `via` (string): names the specific data element or state that connects the two tools. For data dependencies, this is typically a parameter name or return field (e.g., `card_id`, `flight_id`, `booking_id`). For state preconditions, this is typically the state element (e.g., `access_token`, `authenticated_session`).

### Two edge types in detail

**data_dependency** — used when:
- Tool A returns an ID that Tool B takes as input
- Tool A returns a list that Tool B filters or selects from
- Tool A produces a computed value that Tool B requires

Examples:
- `search_flights` returns flight options -> `book_flight` takes a `flight_id` from those results
- `register_credit_card` returns `card_id` -> `book_flight` takes `card_id` as payment
- `book_flight` returns `booking_id` -> `retrieve_invoice` takes `booking_id`
- `book_flight` returns `booking_id` -> `cancel_booking` takes `booking_id`

**state_precondition** — used when:
- Tool A sets up authentication that Tool B requires
- Tool A initializes a session or resource that Tool B depends on
- The dependency is about system state, not a specific data value passed between calls

Examples:
- `authenticate_travel` -> `book_flight` (via `access_token`)
- `initialize_workspace` -> `upload_file` (via `workspace_session`)

### When NOT to create an edge

- Do not create edges between tools that merely share a table. Reading the same table is not a dependency.
- Do not create edges for implicit ordering that any user would know (e.g., "you search before you book" is only an edge if search returns data that book requires as input).
- Do not create self-loops.

### Transitive dependency edges

If tool A feeds tool B and tool B feeds tool C, also add a direct edge from A to C when A's output is directly usable as input to C (independently of B's output). For example, if `register_credit_card` returns `card_id`, `book_flight` uses `card_id`, and `cancel_booking` also accepts `card_id` for refund purposes, then add both `register_credit_card -> book_flight` and `register_credit_card -> cancel_booking`. Only omit the transitive edge when C truly cannot use A's output without B as an intermediary.

## Design Guidelines

### Graph density

Aim for 1.5x to 3x edges per node. This means:
- An environment with 10 tools should have roughly 15-30 edges
- An environment with 20 tools should have roughly 30-60 edges

Too few edges (below 1x) indicates a disconnected graph where tools are isolated — this prevents interesting multi-step tasks. Too many edges (above 3x) indicates over-constraining where the graph becomes a rigid pipeline with no flexibility.

### Structural goals

- Every mutation tool should have at least one incoming edge. Something must provide its required data or establish its required state.
- Query tools that discover data (list, search, get) should feed into mutation tools via data_dependency edges.
- The graph should enable diverse multi-step task paths, not just a single linear chain.
- Multiple valid orderings should exist for common operations.

### Common patterns

Apply these patterns when they match the environment's tool structure:

**Auth fan-out**: An authentication tool connects to many other tools via state_precondition edges. This is typical when most tools require a valid session.
```
authenticate -> tool_1 (state_precondition via access_token)
authenticate -> tool_2 (state_precondition via access_token)
authenticate -> tool_3 (state_precondition via access_token)
```

**Register-then-use**: A registration or creation tool produces an ID consumed by other tools.
```
register_card -> book_flight (data_dependency via card_id)
register_card -> purchase_insurance (data_dependency via card_id)
```

**Query-then-mutate**: A search or listing tool provides options for a mutation tool.
```
search_flights -> book_flight (data_dependency via flight_id)
list_hotels -> reserve_hotel (data_dependency via hotel_id)
```

**Mutate-then-query**: A mutation produces a record that a query tool retrieves.
```
book_flight -> retrieve_invoice (data_dependency via booking_id)
create_order -> get_order_status (data_dependency via order_id)
```

**Mutate-then-mutate**: One mutation's output feeds another mutation.
```
book_flight -> purchase_insurance (data_dependency via booking_id)
create_order -> add_item_to_order (data_dependency via order_id)
```

## Validation Rules

Before finalizing the graph, verify:

1. **Node completeness**: Every tool in `tools.jsonl` has exactly one node. No extras, no missing.
2. **Edge validity**: Every edge's `from` and `to` reference valid node names.
3. **Table validity**: Every table name in `reads` and `writes` arrays exists in `schema.sql`.
4. **Category consistency**: Every node's `category` matches the same tool's category in `tools.jsonl`.
5. **Query write constraint**: Nodes with category `"query"` have empty `writes` arrays.
6. **Mutation write constraint**: Nodes with category `"mutation"` have non-empty `writes` arrays.
7. **No orphan nodes**: Every node participates in at least one edge (as `from` or `to`), except purely standalone utility tools with no dependencies.
8. **No self-loops**: No edge has the same `from` and `to`.
9. **Edge density**: Total edges / total nodes is between 1.5 and 3.0.

## Example

For a travel booking environment with 5 tools:

```json
{
  "nodes": [
    {"name": "authenticate_travel", "category": "mutation", "reads": ["users"], "writes": ["session"]},
    {"name": "search_flights", "category": "query", "reads": ["session", "flight_routes", "airports"], "writes": []},
    {"name": "register_credit_card", "category": "mutation", "reads": ["session"], "writes": ["credit_cards"]},
    {"name": "book_flight", "category": "mutation", "reads": ["session", "credit_cards", "flight_routes"], "writes": ["bookings", "credit_cards"]},
    {"name": "get_booking", "category": "query", "reads": ["session", "bookings"], "writes": []}
  ],
  "edges": [
    {"from": "authenticate_travel", "to": "search_flights", "type": "state_precondition", "via": "access_token"},
    {"from": "authenticate_travel", "to": "register_credit_card", "type": "state_precondition", "via": "access_token"},
    {"from": "authenticate_travel", "to": "book_flight", "type": "state_precondition", "via": "access_token"},
    {"from": "authenticate_travel", "to": "get_booking", "type": "state_precondition", "via": "access_token"},
    {"from": "search_flights", "to": "book_flight", "type": "data_dependency", "via": "flight_id"},
    {"from": "register_credit_card", "to": "book_flight", "type": "data_dependency", "via": "card_id"},
    {"from": "book_flight", "to": "get_booking", "type": "data_dependency", "via": "booking_id"}
  ]
}
```

This example has 5 nodes and 7 edges (ratio 1.4x — acceptable for a small environment). The auth tool fans out to all others. Search and register feed into book. Book feeds into get.

## Important

- Read `tools.jsonl` and `schema.sql` carefully before constructing the graph. Do not invent tool names or table names.
- The graph must be a faithful representation of actual data flow, not aspirational connections.
- Every edge must be justifiable by examining the tools' input parameters and return values.
- Prefer explicit data_dependency edges over state_precondition edges. Only use state_precondition when the connection is truly about system state rather than a passed value.
