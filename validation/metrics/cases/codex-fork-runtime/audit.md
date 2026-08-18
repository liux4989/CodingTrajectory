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
