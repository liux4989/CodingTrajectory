# Claude stream cache audit

## Selection

This case preserves two real Claude provider responses within one turn. The first response appears twice in the JSONL stream—once for reasoning and once for tool use—with the same `message.id`; the audit counts both recorded usage blocks. This intentionally changes the accounting policy from response-ID deduplication to stream-record usage. It does not assert two independent API charges. All prompt, reasoning, response, and tool-result content was replaced.

## Usage arithmetic

The committed source records in `source/session.jsonl:2-3` each contain 34,989 uncached input, 3,712 cached input, and 47 output tokens. Line 5 contains 128 uncached input, 39,616 cached input, and 323 output tokens. Counting each recorded block yields uncached `34,989 * 2 + 128 = 70,106`, cached `3,712 * 2 + 39,616 = 47,040`, completion `47 * 2 + 323 = 417`, and processed `70,106 + 47,040 + 417 = 117,563`. Cache writes remain zero.

Pinned recorded-usage cost estimate is `70,106 * 1.4 / 1,000,000 + 47,040 * 0.26 / 1,000,000 + 417 * 4.4 / 1,000,000 = 0.1122136 USD`. This is not verified invoice cost; repeated stream usage is priced repeatedly.

## Lifecycle

The user prompt begins one turn. Lines 2-3 share provider response id `response-1`, so they are one assistant response containing one reasoning item and one `WebSearch` request; line 4 completes that tool. Line 5 is provider response `response-2` and completes the turn. The semantic totals are therefore two assistant responses, one tool action, one reasoning item, and three runtime items (assistant responses plus tool actions). The sanitized request on line 3 has an empty input object, so its canonical tool action still contributes to runtime metrics but the default overview omits it because it cannot identify the search subject. The old event-envelope tool count of `2` incorrectly treated the separate reasoning stream record on line 2 as another tool action. The session itself is `not_living`: session status now means whether a current turn is running, rather than whether an earlier turn completed. The timestamps span 75 seconds after whole-second rounding.

## Model throughput

The turn spans `13:54:11.521Z` to `13:55:26.766Z` (`session.jsonl:1,5`) = `75.245` seconds. The completed tool interval is `13:54:39.054Z` to `13:54:40.581Z` (`session.jsonl:3,4`) = `1.527` seconds, leaving `73.718` model-active seconds. The recorded processed total is `117,563`, so the source-derived rate is `117,563 / 73.718 = 1,594.767 processed tokens/second`. Repeated usage contributes to this rate; it is not deduplicated physical model throughput.

## Cross-check

Assertions cover per-record usage preservation, cache accounting, one-turn status, tool lifecycle counts, model attribution, and pinned estimated cost.

## Prepared overview v4

The identity from line 1 is now `root_session_id`; the completed turn evidenced
by lines 1 and 5 is now in top-level `turns`. `orchestration.kind` still describes
the one source session. These are field-placement changes, not metric changes.
The bounded activity list retains canonical actions even when their subject is
unavailable; the historical display omission described above is no longer used.

## Output token throughput

The numerator is recorded output tokens: both response-1 stream fragments (47 each, `source/session.jsonl:2,3`) plus response-2 (323, `source/session.jsonl:5`) = 417 output tokens. Over the same model-active denominator as above, the source-derived rate is `417 / 73.718` = 5.657 output tokens/second. Repeated usage is included, so this is not deduplicated generation throughput. It is an end-to-end model-active rate, so time to first token and prefill are included.
