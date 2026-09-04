# Codex Session Metrics Audit

Reviewed on 2026-06-10 against:

- [Codex app-server events](https://developers.openai.com/codex/app-server#events)
- local Codex source at `/Users/cr7sund/Documents/Code/codex`, commit
  `e0cb4ede4e44a371d595520b29d0c80336b8733e`
- installed `codex-cli 0.139.0`
- recent and historical rollout files under `~/.codex/sessions`

## Source Boundary

The app-server documentation describes the stable client-facing JSON-RPC
notifications. CodingTrajectory does not ingest that stream. It ingests the
lower-level persisted rollout records written to `~/.codex/sessions`.

The two surfaces are related but not identical:

| App-server concept | Persisted rollout source | CodingTrajectory use |
| --- | --- | --- |
| `turn/started` | `event_msg.task_started` | turn context window and exact start metadata |
| `turn/completed` | `event_msg.task_complete` | terminal turn status and exact timing |
| interrupted turn | `event_msg.turn_aborted` | interrupted status and duration |
| `thread/tokenUsage/updated` | `event_msg.token_count` | token usage, context window |
| `item/*` tool lifecycle | `response_item` call/output pairs | canonical tool items and output metrics |
| context compaction item | `event_msg.context_compacted` | compaction count |
| `turn/steer` | another `user_message` with the same raw turn id | message in the existing turn |

The Codex source confirms that `TokenUsageInfo` contains:

- `total_token_usage`: cumulative usage for the session;
- `last_token_usage`: usage from the latest model response;
- `model_context_window`: the active model context-window size.

These fields must not be treated as interchangeable.

## Metric Semantics

CodingTrajectory now uses the Codex sources as follows:

| Metric | Source | Semantics |
| --- | --- | --- |
| session and turn token usage | cumulative `total_token_usage` snapshots | deltas between monotonic snapshots |
| current context used | latest `last_token_usage.input_tokens` | latest model request, not session total |
| cached input | Codex token usage | provider-reported cached input |
| output and reasoning | Codex token usage | provider-reported output buckets |
| context-window size | token usage or turn-start event | provider-reported size |
| tool count and outputs | persisted response items | observed tool calls/results |
| tool duration | call/result timestamps | observed elapsed boundary, not model billing |
| turn runtime and TTFT | turn completion/abort events | exact Codex-reported values when present |
| cost | token usage plus pricing table | derived estimate, not a Codex billing record |

Per-tool token cost is not available in the Codex protocol. Token usage arrives
after model responses, so the narrowest honest measured boundary remains the
model generation. Individual tool output size and duration are causal signals, not
independent billing records.

## Fixed In This Audit

### Missing tool forms

The adapter previously handled only `function_call` and
`function_call_output`. Real logs also contain:

- `custom_tool_call` and `custom_tool_call_output`, including `apply_patch`;
- `tool_search_call` and `tool_search_output`;
- `web_search_call`;
- `local_shell_call`;
- `image_generation_call`.

Dropping these records undercounted tool calls, tool outputs, code-change
activity, and context contributors. One validated session changed from 78 to 93
tool calls after its 15 persisted `apply_patch` calls were included.

### Incorrect turn boundaries

The adapter previously started a new turn for every `user_message`. Codex
`turn/steer` adds another user message to the active turn without emitting a
new turn start. The adapter now groups user messages by the persisted raw turn
id. A validated session changed from 6 inferred turns to 5 actual Codex turns.

### Interrupted turns and rollbacks

`turn_aborted` now closes the active turn as interrupted and preserves the
Codex-reported reason and duration. `thread_rolled_back` is retained as a
runtime observation and exposed as a rollback count.

### Runtime facts

The adapter now preserves:

- Codex turn id and trace id;
- turn duration;
- time to first token;
- abort reason;
- rolled-back turn count.

Session stats expose interrupted turns, rollbacks, observed model-turn runtime,
and average time to first token when the source records provide them.

## Remaining Weaknesses

### 1. Failed-turn semantics are incomplete

Codex persists `error` events with structured `codex_error_info`, while the
canonical turn model currently has no `failed` status. A failed model request
can therefore be reported as incomplete or interrupted rather than failed.

Direction: add canonical failed turn/session status and map terminal Codex
errors without treating non-terminal warnings as failures.

### 2. Rollback cost and active context are not separated

Rolled-back work still consumed tokens and should remain in session cost.
However, the current context analysis does not distinguish:

- historical billed work that was rolled back;
- messages still present in the active conversation context.

Direction: preserve both ledgers. Never subtract rolled-back usage from billed
session totals, but mark rolled-back turns as excluded from active-context
composition.

### 3. Model reroutes are not reflected in pricing

Codex can emit `model_reroute` when the backend changes models. Current pricing
uses the model from turn context. A rerouted turn could therefore use the wrong
price rule.

Direction: normalize model-routing observations and attach the effective model
to subsequent usage observations.

### 4. App-server item coverage is broader than rollout parsing

The documented item union includes command execution, file changes, MCP,
dynamic tools, collaboration, web search, image viewing, review mode, and
compaction. The adapter covers the common persisted response-item forms, but
does not yet normalize every `event_msg.item_completed` variant or every
specialized lifecycle event.

Direction: add a source-versioned item decoder, prefer authoritative completed
items, and deduplicate them against legacy response-item records.

### 5. Raw rollout compatibility is implicit

The app-server can generate a version-matched JSON Schema, but persisted rollout
JSONL is a lower-level internal surface. Unknown record types are currently
ignored silently.

Direction: record source `cli_version`, maintain a known-event coverage report,
and emit ingestion warnings when an unknown record could affect hierarchy,
status, tools, usage, or pricing.

### 6. Structured tool outputs are reduced for text metrics

Codex tool outputs may be strings or structured content items containing text
and images. CodingTrajectory preserves the raw value, but output-size and
success inference remain text-oriented.

Direction: normalize structured output content into explicit text, image, and
encrypted-content observations before calculating output metrics.

## Priority Order

1. Add failed-turn status and structured error ingestion.
2. Separate billed history from active context after rollback.
3. Track effective model after reroutes.
4. Add versioned completed-item decoding and unknown-event warnings.
5. Improve structured and multimodal tool-output metrics.

