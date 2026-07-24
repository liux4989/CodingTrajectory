# Codex inter-agent-triggered turn audit

## Selection

This case covers a Codex forked subagent continuation window whose turns are triggered by inter-agent communication rather than a user message. The fork source file (`source/fork.jsonl`) deliberately contains **no `user_message` event**: its turns begin on `task_started` boundaries and end on `task_complete`, bracketed by a stable `turn_id`. It also carries two inherited orphan boundary markers from the fork source - a `task_started` whose matching `task_complete` lives in another file, and a `task_complete` whose matching `task_started` lives in another file. User, assistant, and tool content was sanitized; session relations, timestamps, turn_ids, runtime observations, and usage buckets were retained.

This exercises the projector fix that brackets turns with `task_started`/`task_complete` lifecycle events (by `turn_id`) for transcripts that have no `user_message`, instead of dropping all their work.

## Inter-agent-triggered turns

`source/fork.jsonl` has no `user_message`. Its turns are delimited by lifecycle events:

- `fork.jsonl:5` `task_started` turn `…000002` is matched by `fork.jsonl:10` `task_complete` turn `…000002` -> turn 1.
- `fork.jsonl:11` `task_started` turn `…000003` is matched by `fork.jsonl:16` `task_complete` turn `…000003` -> turn 2.

The two orphan markers must not produce turns:

- `fork.jsonl:3` `task_started` turn `…000099` has no matching `task_complete` in this file -> skipped (inherited from the fork source).
- `fork.jsonl:4` `task_complete` turn `…000098` has no matching `task_started` in this file -> ignored as a terminal (it must not close turn 1, whose `turn_id` differs).

With the lifecycle bracketing the fork reconstructs **2 turns** and **4 items** (one `custom_tool_call` plus one `custom_tool_call_output` per turn, at `fork.jsonl:7-8` and `fork.jsonl:13-14`). Before the fix the projector keyed turns off `user_message` only, so this file reconstructed **0 turns / 0 items** while its event-sourced `tool_calls` (2) and billed tokens stayed correct.

## Graph and runtime

`source/fork.jsonl:1` names the parent through `forked_from_id` `…000001`, so the graph has one main session and one `forked_from` subagent.

- Parent turn (`parent.jsonl:2` -> `parent.jsonl:6`): spans `08:51:06.978Z` to `08:52:55.745Z` = 108.767 s, rounded to 109 s.
- Fork turn 1 (`fork.jsonl:5` -> `fork.jsonl:10`): spans `10:19:16.799Z` to `10:20:16.799Z` = 60.000 s, rounded to 60 s.
- Fork turn 2 (`fork.jsonl:11` -> `fork.jsonl:16`): spans `10:20:17.000Z` to `10:20:47.000Z` = 30.000 s, rounded to 30 s.

Graph execution_seconds = 109 + 60 + 30 = **199**. The fork contributes 2 of the 3 graph turns and 2 of the 2 tool calls (`fork.jsonl:7`, `fork.jsonl:13`).

Time-to-first-token is averaged across the three completed-turn observations with a non-null ttft: parent 3,392 ms (`parent.jsonl:6`), fork turn 1 2,000 ms (`fork.jsonl:10`), fork turn 2 1,500 ms (`fork.jsonl:16`). The orphan terminal at `fork.jsonl:4` carries no `time_to_first_token_ms`, so it is excluded. Average = round((3392 + 2000 + 1500) / 3) = round(2297.33) = **2297**.

## Session scope

The `session.*` surfaces now select the entrypoint thread. For this case the
entrypoint is the parent in `source/parent.jsonl:1`, so session metrics contain
only its one completed turn: 109 rounded execution seconds, zero subagents and
zero tools, 3,392 ms TTFT, 108.767 model-active seconds, and 1,110 processed
tokens. Its uncached/cached/completion/reasoning buckets are 200/800/100/10,
which produce 1,110 processed tokens and the pinned 0.0047 USD cost. The two
inter-agent-triggered child turns remain graph evidence and are exposed by the
explicit `graph.*` surfaces.

## Usage arithmetic

Each `token_count` observation reports `last_token_usage` equal to `total_token_usage` (single observation per turn). Codex input includes cached input, so uncached input = `input_tokens - cached_input_tokens`, and the canonical processed total is `uncached + cached + completion + reasoning` (= `input + completion + reasoning` since cache_write is 0).

| Source | input | cached | output | reasoning | uncached | processed |
|---|---|---|---|---|---|---|
| `parent.jsonl:5` | 1000 | 800 | 100 | 10 | 200 | 1110 |
| `fork.jsonl:9` (turn 1) | 2000 | 1800 | 80 | 8 | 200 | 2088 |
| `fork.jsonl:15` (turn 2) | 1500 | 1400 | 50 | 5 | 100 | 1555 |
| **graph total** | 4500 | 4000 | 230 | 23 | 500 | 4753 |

Graph billed processed = 1110 + 2088 + 1555 = **4753**; uncached_prompt = 200 + 200 + 100 = **500**; cached_prompt = **4000**; completion = **230**; reasoning = **23**.

Pinned cost (gpt-5.5: input 5.0, cached 0.5, output 30.0, reasoning 30.0 per 1M tokens; Codex uses uncached input):

`500 * 5.0 / 1,000,000 + 4000 * 0.5 / 1,000,000 + 230 * 30.0 / 1,000,000 + 23 * 30.0 / 1,000,000 = 0.0025 + 0.002 + 0.0069 + 0.00069 = 0.01209 USD`.

## Model throughput

The parent turn has `108.767` active seconds (`parent.jsonl:2,6`) and no tool interval. Fork turn 1 spans `60.000` seconds (`fork.jsonl:5,10`) and its observed tool interval is `0.100` seconds (`fork.jsonl:7,8`), leaving `59.900`. Fork turn 2 spans `30.000` seconds (`fork.jsonl:11,16`) and its observed tool interval is `0.500` seconds (`fork.jsonl:13,14`), leaving `29.500`. The graph denominator is therefore `108.767 + 59.900 + 29.500 = 198.167` model-active seconds. With `4,753` processed tokens, the source-derived common rate is `4,753 / 198.167 = 23.985 processed tokens/second`, including `23` reasoning tokens.

## Cross-check

The expected artifacts assert the fork's 2-turn / 4-item reconstruction (the regression guard: 0 before the fix), the `forked_from` graph relation, the 3-turn / 199-second / 2297-ms-ttft graph runtime, the usage buckets, and the pinned cost. Presentation-only text and generated item identifiers are intentionally omitted.
