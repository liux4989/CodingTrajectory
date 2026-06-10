# Item Token Attribution Refactor

Status: complete

Date: 2026-06-10

Completed: 2026-06-10

## Problem

CodingTrajectory has confident token accounting at the model request/response
boundary. In current public surfaces this is represented at turn/session level:

- `session.usage`
- `session.turn_usage`
- `session.stats`

Item-level token cost is different. Codex persisted logs do not attach
`token_count` records to item IDs or tool call IDs. A `response_item` tool call
has a `call_id`, but the later `event_msg.token_count` has only
`last_token_usage`, `total_token_usage`, and context/quota metadata. Therefore
per-item token cost cannot be measured directly from Codex logs.

The current safe boundary is:

- observed turn/session usage: real provider-reported token buckets;
- item/tool attribution: derived estimate based on visible item content and
  event order.

## Non-Goals

- Do not move turn/session usage to item scope.
- Do not claim item attribution is exact billing truth.
- Do not allocate cached-token reuse to individual items in the first pass.
- Do not introduce USD cost into item/tool attribution.
- Do not make plugin consumers depend on core-private helper APIs.

## Current Surfaces

### `session.turn_usage`

Purpose: inspect real usage for one turn.

Keep this surface unchanged. It should remain the highest-confidence answer for
the token cost of a user turn. It includes cache-aware token buckets exactly as
reported or normalized:

- `input_tokens`
- `cached_input_tokens`
- `cache_creation_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`
- `total_tokens`

### `session.usage`

Purpose: compact session and turn-level token accounting, plus cost only when
the source session log reports it.

Keep this surface unchanged. It should not expose item attribution because that
would mix measured provider usage with estimated visible-content attribution.

### `session.stats`

Purpose: context-window composition, category breakdown, quota, runtime, and
message stats.

Keep this surface cache-aware and turn/session-scoped. Documentation and labels
may clarify that category composition combines exact usage buckets with
estimated structural/text evidence, but the API should not become item-level.

### `session.tool_usage`

Purpose today: tool invocation statistics and tool output diagnostics.

This is the correct place to add item-level token attribution. It is already an
item-oriented diagnostic view and does not currently claim billing precision.

## Target Semantics

Use two ledgers:

1. **Observed usage ledger**
   - Source: provider token usage observations.
   - Scope: turn/session.
   - Cache-aware.
   - Authoritative for total usage.

2. **Item attribution ledger**
   - Source: visible item content plus event-order attribution.
   - Scope: item/tool.
   - Cache-agnostic in the first pass.
   - Diagnostic, not authoritative.

## Proposed Tool Attribution Fields

Extend each `ToolItemFlat` emitted by `session.tool_usage` with token-only
diagnostics:

```json
{
  "item_id": "...",
  "session_id": "...",
  "turn_id": "...",
  "tool_name": "exec_command",
  "input_summary": "sed -n '1,160p' file.py",
  "output_chars": 8421,
  "output_original_tokens": 2419,
  "token_attribution": {
    "tool_input_tokens": 22,
    "tool_output_tokens": 2419,
    "content_confidence": "observed_tool_output_token_count",
    "method": "visible_content_estimate"
  },
  "invoke_response_tokens": {
    "output_tokens": 82,
    "reasoning_output_tokens": 11,
    "attribution": "shared_model_response"
  },
  "read_after_result": {
    "included_in_turn_usage": true,
    "attribution": "causal_next_model_request"
  }
}
```

Field meaning:

- `tool_input_tokens`: estimated tokens in serialized tool arguments or command.
- `tool_output_tokens`: estimated visible tool-result tokens.
- `content_confidence`: why the output-token estimate is trusted.
- `invoke_response_tokens`: optional allocation of output/reasoning tokens from
  the model response that requested the tool.
- `read_after_result`: whether the tool result was followed by another model
  request in the same turn, meaning the result could have contributed to that
  request's input context.

Do not add `cached_input_tokens_by_item` or `cache_creation_input_tokens_by_item`
in the first pass. Cache attribution requires repeated-content matching and
would make the item view harder to explain.

## Attribution Rules

### Visible Content

Estimate visible content tokens from the item itself:

- tool input: serialized `input` or command string;
- tool output: returned output text;
- assistant message: message text;
- reasoning item: visible reasoning text, when present.

For tool output, prefer an observed wrapper count when available. Existing
Codex tool outputs may include:

```text
Original token count: N
```

When that marker exists, use it as `tool_output_tokens` and set confidence to an
observed wrapper count. Otherwise estimate from text length.

### Invoke Response

A model response may emit one or more tool calls. The next `token_count`
observation after those `response_item` tool-call records describes the whole
model response, not each individual call.

Attribution policy:

- one tool call in the response: assign output/reasoning usage to that item with
  `attribution = "single_tool_response"`;
- multiple tool calls in the response: split or mark shared with
  `attribution = "shared_model_response"`;
- no adjacent usage observation: omit the field or use
  `attribution = "unknown"`.

### Read After Result

Tool output is not token cost while the tool executes. It becomes model input
only if a later model request sees it. Normally this happens inside the same
turn.

Attribution policy:

- if a tool result is followed by another `token_count` before turn completion,
  set `included_in_turn_usage = true`;
- if the turn completes or aborts before another model request, set
  `included_in_turn_usage = false`;
- do not allocate cached-token reuse back to the item.

## Reconciliation

Item attribution should not be forced to sum to turn usage.

Reasons:

- hidden system/developer/runtime context contributes to turn usage;
- tool schemas and runtime framing may contribute to request usage;
- one model response can both read a prior tool result and invoke the next tool;
- cached context can reappear in provider buckets without a simple visible-item
  owner;
- visible tool output size is not the same thing as billable input buckets.

The `session.tool_usage` payload should include a note or metadata block:

```json
{
  "attribution_policy": {
    "scope": "tool_items",
    "cache": "not_allocated_to_items",
    "usage_authority": "session.usage",
    "method": "visible_content_plus_event_order"
  }
}
```

## Plugin And UI Impact

### Dashboard

The context-window dashboard should keep provider capacity/usage separate from
observed semantic composition. Context composition and tool attribution share
the same visible-content sizing logic; tool attribution can remain an optional
drill-down for tool rows.

Do not replace existing turn totals with item sums.

### Activity Plugin

No immediate change. It currently consumes session list output, not the detailed
metrics API.

### Review Plugin

No immediate contract change. The review plugin may include tool attribution as
additional evidence later, but judge metrics should continue to use observed
session/turn usage.

### CLI

`ct session usage` should stay turn-focused.

If a CLI surface for `session.tool_usage` is added or exposed, render item
attribution with labels such as:

- `tool input`
- `tool output`
- `invoke response`
- `read after result`
- `estimated`
- `shared`

Avoid labels such as `real item cost` or `per-tool billing`.

## Implementation Status

Completed:

1. Added token-only attribution models to `metrics.models`.
2. Extended `build_session_graph_tool_usage()` with visible-content token
   estimates.
3. Added event-order grouping for adjacent model-response usage observations.
4. Added payload-level `attribution_policy` metadata.
5. Updated `docs/architecture.md`, `docs/cli.md`, and `docs/plugin.md`.
6. Validated against real Codex sessions containing multiple tool calls in one
   model response and tool outputs with `Original token count`.

`read_after_result` requires a usage observation after both the tool result and
the invoking response's usage observation. This prevents a late invocation
usage event from being mistaken for evidence that the tool result was read by a
subsequent model request.

## Validation Expectations

Use real sessions rather than synthetic-only fixtures.

Expected behavior:

1. `session.usage` and `session.turn_usage` remain token-focused and do not
   estimate missing prices in core.
2. `session.stats` remains cache-aware.
3. `session.tool_usage` includes token attribution only when evidence exists.
4. Multi-tool responses are marked shared rather than duplicated as exact item
   usage.
5. Tool output token estimates prefer observed wrapper counts over rough text
   estimates.
6. Item attribution metadata clearly states that cache reuse is not allocated to
   items.
