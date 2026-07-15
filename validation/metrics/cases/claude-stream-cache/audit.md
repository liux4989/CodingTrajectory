# Claude stream cache audit

## Selection

This case preserves two real Claude provider responses within one turn. The first response appears twice in the JSONL stream—once for reasoning and once for tool use—with the same `message.id`; the audit counts its usage once. All prompt, reasoning, response, and tool-result content was replaced.

## Usage arithmetic

Response 1 in `source/session.jsonl:2-3` contributes one observation: 34,989 uncached input, 3,712 cached input, and 47 output tokens. Response 2 in line 5 contributes 128 uncached input, 39,616 cached input, and 323 output tokens. Totals are uncached `34,989 + 128 = 35,117`, cached `3,712 + 39,616 = 43,328`, completion `47 + 323 = 370`, and processed `35,117 + 43,328 + 370 = 78,815`.

Pinned cost is `35,117 * 1.4 / 1,000,000 + 43,328 * 0.26 / 1,000,000 + 370 * 4.4 / 1,000,000 = 0.06205708 USD`.

## Lifecycle

The user prompt begins one turn. The duplicated first provider response emits reasoning plus one `WebSearch` request, line 4 completes that tool, and response 2 completes the turn. The timestamps span 75 seconds after whole-second rounding.

## Cross-check

Assertions cover response de-duplication, cache accounting, one-turn status, tool lifecycle counts, model attribution, and pinned estimated cost.
