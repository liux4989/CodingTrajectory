# Pi reported cost audit

## Selection

This case preserves one real Pi turn containing five provider calls and four successful shell tool lifecycles. Prompt, reasoning, assistant, and tool-result content was replaced without changing timestamps, usage, reported costs, model changes, or call/result relationships.

## Usage arithmetic

The five assistant records at `source/session.jsonl:5,7,9,11,13` report uncached inputs 2,551, 1,185, 167, 178, and 122, totaling 4,203. Cached inputs 0, 2,560, 3,776, 3,968, and 4,160 total 14,464. Outputs 65, 47, 52, 53, and 50 total 267. Processed usage is `4,203 + 14,464 + 267 = 18,934`; prompt plus completion is `4,203 + 267 = 4,470`.

## Reported cost

The same five records report USD totals 0.00385740, 0.00253140, 0.00144436, 0.00151408, and 0.00147240. Their sum is 0.01081964 USD. Because the source supplies cost evidence for every provider call, confidence is `reported` and the pinned pricing artifact is not used for this case's cost value.

## Lifecycle

The user message starts one turn. Four assistant `bash` calls are paired with four successful `toolResult` records, followed by a final assistant response. The visible timestamps span 91 seconds after whole-second rounding.

## Cross-check

Assertions cover token sums, reported cost and confidence, successful tool counts, one-turn status, runtime, and single-model attribution.
