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
