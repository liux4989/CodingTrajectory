# Codex fork runtime audit

## Selection

This case preserves one completed parent turn and one later forked session from a real Codex CLI graph. User, assistant, and tool content was replaced; session relations, timestamps, runtime observations, model identity, and the selected final usage observation were retained.

## Usage arithmetic

The selected parent observation in `source/parent.jsonl:5` reports input 78,203, cached input 75,648, output 937, and reasoning output 42. Codex input includes cached input, so uncached input is `78,203 - 75,648 = 2,555`. The canonical processed total is `2,555 + 75,648 + 937 + 42 = 79,182`. The provider-reported total is `78,203 + 937 = 79,140`; the 42-token difference is the separately exposed reasoning bucket.

Pinned cost is `2,555 * 5.0 / 1,000,000 + 75,648 * 0.5 / 1,000,000 + 937 * 30.0 / 1,000,000 + 42 * 30.0 / 1,000,000 = 0.079969 USD` under the core contract's reasoning-inclusive processed accounting.

## Model throughput

The parent turn spans `08:51:06.978Z` to `08:52:55.745Z` (`parent.jsonl:2,6`) = `108.767` model-active seconds. It has no observed tool interval. The fork turn has no token usage, so it is excluded from the usage-bearing throughput denominator. The source-derived rate is `79,182 / 108.767 = 727.997 processed tokens/second`; reasoning is included in the processed numerator.

## Graph and runtime

`source/fork.jsonl:1` names the parent only through `forked_from_id`; it has no
`thread_spawn.parent_thread_id`. The source therefore proves an ordinary
conversation fork, not a spawned-agent relationship. The unified lineage has
two completed turns, while each branch-local orchestration graph contains one
session. Provider durations are 108.802 seconds for the parent and 0.827 seconds
for the separate fork. The parent supplies one time-to-first-token observation
of 3,392 ms.

## Session scope

The `session.*` surfaces now select the entrypoint thread. For this case that is
the parent in `source/parent.jsonl:1`; its one turn spans 108.767 active seconds,
rounds to 109 execution seconds, and has no subagent or tool items. The fork
relationship remains available from `session.tree`; the separate fork's
one-second duration is available by selecting that branch. It is not aggregated
into the parent's `graph.*` surfaces.

## Cross-check

The expected artifacts assert the parent-thread session boundary and preserve
the fork relation through the conversation-tree projection. Presentation-only
text and generated item identifiers are intentionally omitted.

## Prepared overview v4

The same source evidence now appears at `root_session_id`, top-level `turns`,
and `orchestration`. Parent line 1 has no parent, hence relationship `root`
(formerly the presentation role `main`). Lines 2–6 prove the single turn's
zero-based source ordinal 0 and completed status. Replace the duplicate status
assertion with that ordinal. No usage, cost, runtime or membership value changes.

## Output token throughput

The numerator is generated (output) tokens: `last_token_usage.output_tokens` is 937 at `source/parent.jsonl:5` (reasoning included). Over the same model-active denominator as above, the source-derived rate is `937 / 108.767` = 8.615 output tokens/second. It is an end-to-end model-active rate, so time to first token and prefill are included.

## Execution split

The root turn spans 108.767 seconds (source/parent.jsonl:2,6) with no tool calls. Its split is therefore 108.767 seconds of estimated LLM time and zero tool seconds. The separate fork (source/fork.jsonl:2,5) spans 0.827 seconds with no tools or usage; execution timing remains measurable even without token usage.

These additive assertions derive from committed timestamps, not command output. Existing execution and throughput expectations remain unchanged. Execution uses whole-second rounding per turn; split fields retain millisecond precision. The arithmetic extends the source audit and does not constitute independent organizational sign-off.


## Recorded cost and net cache savings

The committed usage counts at `source/parent.jsonl:5` and the pinned pricing artifact produce `total_cost = 0.079969` USD (estimated). Repricing the same input without cache reads yields `0.420385` USD, with output and separately counted reasoning unchanged. There are no cache writes in this case. Net savings is `0.420385 - 0.079969 = 0.340416` USD. The summary includes 1 recorded usage entries; repeated Claude stream records remain repeated recorded evidence. Existing expected metrics are unchanged.


## Session narrative v8

The approved session overview refactor selects the exact session and exposes
`session_id`, `title`, ordered `turns`, and selection coverage. The committed
header identifies the same session as before. Session metadata assertions
(vendor, status, or root relationship) move unchanged to `graph.overview` in
`expected/structure.json`; they are no longer session-narrative properties.
The first source turn has canonical sequence 0 under the shared transcript
projector's zero-based turn counter. Its observed completion is unchanged.
Requests and assistant/activity entries retain bounded previews, truncation
flags, and stable evidence references; item details own execution evidence.

No usage, cost, runtime, throughput, or canonical membership value changes.


## Overview property names v9

This is a naming-only migration of session.overview v8: source_sequence becomes
sequence, user_request becomes request, entries becomes items, kind becomes
type, activity_type becomes tool, preview becomes text, and
coverage.selection_trimmed becomes coverage.truncated. Stable session, turn,
item, and event reference names remain explicit. Text remains a bounded preview.
All assertion values and source references remain unchanged. The evidence and
normalization audited under Session narrative v8 still apply; no metric value,
source ordering, outcome, or coverage semantics changes.


## Compact stats execution

Stats runtime.tools is renamed runtime.tool_items, with its original audited
value unchanged. This flat-item count is distinct from observed execution calls.
The selected session evidence at source/parent.jsonl:2-6 contains 0 direct
observed tool calls and no script-executor source or source-only call sites.
Thus execution.scripts is 0, execution.observed_calls is 0, and
execution.source_only_calls is 0. Native calls are counted from execution-unit
references; script wrappers count separately and their source-only inventories
never contribute to observed calls. These assertions derive from committed
source evidence, not current output. Usage, cost, timing, and outcome values
are unchanged. Full execution evidence remains available through session.items.
