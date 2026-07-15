# Codex fork runtime audit

## Selection

This case preserves one completed parent turn and one later forked session from a real Codex CLI graph. User, assistant, and tool content was replaced; session relations, timestamps, runtime observations, model identity, and the selected final usage observation were retained.

## Usage arithmetic

The selected parent observation in `source/parent.jsonl:5` reports input 78,203, cached input 75,648, output 937, and reasoning output 42. Codex input includes cached input, so uncached input is `78,203 - 75,648 = 2,555`. The canonical processed total is `2,555 + 75,648 + 937 + 42 = 79,182`. The provider-reported total is `78,203 + 937 = 79,140`; the 42-token difference is the separately exposed reasoning bucket.

Pinned cost is `2,555 * 5.0 / 1,000,000 + 75,648 * 0.5 / 1,000,000 + 937 * 30.0 / 1,000,000 + 42 * 30.0 / 1,000,000 = 0.079969 USD` under the core contract's reasoning-inclusive processed accounting.

## Graph and runtime

`source/fork.jsonl:1` names the parent through `forked_from_id`. The parent and fork each contain one completed task, so the graph contains two completed turns. Provider durations are 108.802 seconds and 0.827 seconds, rounded per turn to 109 and 1 seconds and summed to 110 seconds. The parent supplies one time-to-first-token observation of 3,392 ms.

## Cross-check

The expected artifacts assert the public graph relation, two-session/two-turn boundary, runtime totals, usage buckets, model attribution, and pinned cost. Presentation-only text and generated item identifiers are intentionally omitted.
