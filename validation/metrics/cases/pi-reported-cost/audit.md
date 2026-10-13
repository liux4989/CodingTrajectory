# Pi reported cost audit

## Selection

This case preserves one real Pi turn containing five provider calls and four successful shell tool lifecycles. Prompt, reasoning, assistant, and tool-result content was replaced without changing timestamps, usage, reported costs, model changes, or call/result relationships.

## Usage arithmetic

The five assistant records at `source/session.jsonl:5,7,9,11,13` report uncached inputs 2,551, 1,185, 167, 178, and 122, totaling 4,203. Cached inputs 0, 2,560, 3,776, 3,968, and 4,160 total 14,464. Outputs 65, 47, 52, 53, and 50 total 267. Processed usage is `4,203 + 14,464 + 267 = 18,934`; prompt plus completion is `4,203 + 267 = 4,470`.

## Reported cost

The same five records report USD totals 0.00385740, 0.00253140, 0.00144436, 0.00151408, and 0.00147240. Their sum is 0.01081964 USD. Because the source supplies cost evidence for every provider call, confidence is `reported` and the pinned pricing artifact is not used for this case's cost value.

## Lifecycle

The user message starts one turn. Four assistant `bash` calls are paired with four successful `toolResult` records, followed by a final assistant response with `stopReason: stop`. The session itself is `not_living`: session status now means whether a current turn is running, rather than whether an earlier turn completed. The visible timestamps span 91 seconds after whole-second rounding.

The four source-paired `bash` lifecycles at `source/session.jsonl:5-12` are adjacent, agent-produced, and explicitly successful (`isError: false` on every paired `toolResult`). The activity-cell migration on 2026-08-25 groups that exact canonical success sequence into one `RunCommand` cell with `count: 4`, retaining the four item IDs for drill-down. This is not a wrapper-success inference: Pi records one direct tool lifecycle per command and its paired result supplies the completion evidence.

## Model throughput

The turn spans `11:55:35.457Z` to `11:57:06.918Z` (`session.jsonl:4,13`) = `91.461` seconds. The four completed tool intervals (`session.jsonl:5-12`) total `0.081 + 0.056 + 0.023 + 0.074 = 0.234` seconds, leaving `91.227` model-active seconds. The processed total is `18,934`, so the source-derived rate is `18,934 / 91.227 = 207.548 processed tokens/second`.

## Cross-check

Assertions cover token sums, reported cost and confidence, successful tool counts, one-turn status, runtime, and single-model attribution.

## Prepared overview v5

Source lines 5–12 independently prove four `bash` call/result pairs. The new
overview restores the shared activity projector described in Lifecycle above:
all four adjacent successful calls form one cell. `activities[0].tool` is
`RunCommand`, its `count` is 4, and `content_coverage.activities.total` is 1.
This replaces the v4 per-item projection; all four canonical items remain
available for drill-down. Identity, turn status and session count remain at `root_session_id`,
`turns[0].status` and `orchestration.session_count`. No usage, cost or runtime
expected value is derived anew from command output.

## Flat command overview

Preparation v6 no longer groups commands in the overview. Source lines 5, 7,
9, and 11 each contain a `bash` call with empty `arguments`: sanitization retained
the lifecycle but no command subject. Once flattened, all four are subjectless
singletons and the existing low-value visibility gate omits them. Therefore
overview activity total and returned are both zero, while canonical tool counts,
usage, cost, and runtime remain unchanged. This expectation follows the committed
source and visibility rule, not regenerated command output.

## Output token throughput

The numerator is generated (output) tokens: the five assistant messages (`source/session.jsonl:5,7,9,11,13`) report 65+47+52+53+50 = 267 output tokens. Over the same model-active denominator as above, the source-derived rate is `267 / 91.227` = 2.927 output tokens/second. It is an end-to-end model-active rate, so time to first token and prefill are included.

## Identity-preserving tool visibility

The generic visibility rule supersedes the subjectless omission in Flat command
overview. A missing subject does not erase a recorded action identity. Committed
lines 5, 7, 9, and 11 each identify a `bash` tool call; lines 6, 8, 10, and 12
respectively supply the matching `toolCallId` and `isError: false`. The established
shell mapping gives `RunCommand`, and each paired result proves `succeeded`.
Empty sanitized arguments provide no command description, so none is invented.
With command flattening there are four activities in source order: four
`RunCommand` labels and four `succeeded` outcomes. Activity total and returned
are both 4. Each asserted outcome references its own committed call/result pair.

The prior gate failure is retained as `tool-visibility-migration-failure.json`.
The new assertions were reconstructed from these source pairs rather than copied
from the report. Source hashes, call IDs, runtime counts, usage, and costs remain
unchanged. The source and projection cross-check is not independent
organizational sign-off.

## Generic tool identities

Predefined display concepts are now disabled for all tools, including familiar
ones. Committed lines 5, 7, 9, and 11 each contain `name: bash`. The four overview
labels therefore become `bash` rather than the former inferred `RunCommand`.
Each matched result on lines 6, 8, 10, and 12 still proves success through
`toolCallId` and `isError: false`. The generic identity rule does not group these
four lifecycles into a semantic command cell. Counts remain 4, outcomes remain
`succeeded`, and usage, runtime, and cost expectations remain unchanged.

The gate failure is preserved in `generic-tool-labels-migration-failure.json`.
Replacement labels were read from committed source name fields, not from that
report. Source hashes are unchanged. This source-based cross-check does not
constitute independent organizational sign-off.

## Execution split

The turn spans 91.461 seconds (source/session.jsonl:4,13). The four tool intervals (lines 5–12) total 0.081 + 0.056 + 0.023 + 0.074 = 0.234 seconds, leaving 91.227 seconds of estimated LLM time.

These additive assertions derive from committed timestamps, not command output. Existing execution and throughput expectations remain unchanged. Execution uses whole-second rounding per turn; split fields retain millisecond precision. The arithmetic extends the source audit and does not constitute independent organizational sign-off.


## Recorded cost and net cache savings

The committed usage counts at `source/session.jsonl:5,7,9,11,13` and the pinned pricing artifact produce `total_cost = 0.01081964` USD (reported). Repricing the same input without cache reads yields `0.0273086` USD, with output and separately counted reasoning unchanged. There are no cache writes in this case. Net savings is `0.0273086 - 0.01081964 = 0.01648896` USD. The summary includes 5 recorded usage entries; repeated Claude stream records remain repeated recorded evidence. Existing expected metrics are unchanged.


## Complete local overview contract

The complete-local refactor removes `content_coverage.activities` counters and
collection caps. Committed source lines 5–12 still prove the same four successful
`bash` call/result pairs, each already asserted positionally in `activities[0]`
through `activities[3]`. No tool, usage, cost, runtime or throughput expectation
changes.

The two obsolete counter assertions are replaced by display-completeness checks:
`coverage.selection_trimmed` is false because this fixture has one source turn
(lines 4–13) and the overview request has no explicit selection limit.
`turns[0].text_trimmed` is false because the committed sanitized request, response,
reasoning and tool content (lines 4–13) are individually shorter than the retained
280-character display preview limit. These expectations follow the committed
fixture and approved contract, rather than current metric command output.


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

For Pi, committed source/session.jsonl lines 5-12 prove four ordered bash
call/result pairs before the final assistant text on line 13. Their names and
`succeeded` outcomes move unchanged from activities[0-3] to entries[0-3].
The short request on line 4 proves user_request.truncated is false; the
unlimited single-turn selection remains complete. These expectations derive
from the committed evidence and normalization rules, not current CLI output.
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
