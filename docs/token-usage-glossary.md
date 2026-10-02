# Token usage glossary

Keep provider-reported values separate from derived values. Provider totals use
different rules. Pi includes cached prompt tokens in `totalTokens`; Codex can
report reasoning separately. Missing counters do not become zero consumption.

## Buckets and totals

| Field | Meaning | CLI label |
| --- | --- | --- |
| `prompt_tokens` | Prompt/input bucket after source normalization | `prompt` |
| `uncached_prompt_tokens` | Fresh input, excluding cache reads and writes | `input` |
| `cached_prompt_tokens` | Input read from provider cache | `cached` |
| `cache_write_tokens` | Input written to provider cache | `cache write` |
| `completion_tokens` | Output bucket reported by the source | `output` |
| `reasoning_tokens` | Separately reported reasoning/thinking | `reasoning` |
| `reported_total_tokens` | Original provider/log total | `reported` |
| `processed_tokens` | Normalized processed-token total | `processed` |
| `prompt_completion_tokens` | Prompt plus completion | `prompt+completion` |

Processed tokens sum uncached input, cached input, cache writes, completion,
and separately accounted reasoning. Normalization prevents counting an inclusive
provider bucket twice. Preserve the provider total unchanged when it exists.
The prompt bucket can include cache. Use `uncached_prompt_tokens` for fresh input.
Compact CLI JSON can use `prompt_completion` for `prompt_completion_tokens`.

Codex input totals include cache reads and writes. Fresh input is
`max(0, input_tokens - cached_input_tokens - cache_creation_input_tokens)`.
The adapter maps `cache_write_input_tokens` to `cache_creation_input_tokens`,
which public responses expose as `cache_write_tokens`. This applies to
per-response and cumulative observations. All-cached input retains zero fresh input.
Do not infer unreported cache writes from cache misses.

CLI bucket lines omit zero cache and reasoning buckets. Allocation columns use
the order `input/cached/cache write/output/reasoning`. Audit lines can also show
the reported and prompt-plus-completion totals.

## Cost evidence and allocation

Provider-reported cost is native evidence. Catalog-priced cost is an estimate.
Do not price a total token count with one rate; use its component buckets.
Each request estimate uses that request's pricing tier and prompt size.
Turn, model, and session estimates sum request estimates, not aggregate-tier calculations.

`session.request_usage` provides the request ledger. `session.tool_usage`
provides derived item/tool allocation. Each usage observation is allocated
among visible items in the same turn, weighted by visible item tokens.
Later turns cannot change earlier-turn allocation. Allocated slices use the
source request's pricing tier and reconcile to that request's cost estimate.
Allocation is attribution, not a replacement for observed provider totals.

Request/tool-result links can associate observations by timestamp window.
They do not prove that the provider received a linked result; input membership
remains unknown when the source cannot establish it.

## Model-active time

`model_active_seconds` uses observed turn boundaries minus the union of completed
tool intervals. Overlapping tool intervals are subtracted once.
Claude uses a `turn_duration` record when available, not the next user prompt.
This excludes inter-turn user idle time.

An unclosed tool interval makes the turn ineligible. A mixed-model turn cannot
assign its full duration to the dominant model. An aggregate rate requires a
reconstructable denominator. Model-active time is not provider decoder-busy time.

### Processed throughput

`processed_tokens_per_second` is `processed_tokens / model_active_seconds`.
It excludes tool-output token estimates and tool monetary cost.
A rate over full turn duration includes tool execution and must use a different label.

### Output throughput

`output_tokens_per_second` is `completion_tokens / model_active_seconds`.
Its denominator still includes prefill and first-token latency.
Codex includes reasoning in its output bucket. Providers that report reasoning
separately do not add it to this numerator.

### Codex decode estimate

`decode_tokens_per_second` estimates tool-argument decoding from gaps between
completed response items in the same response:

```text
sum(tool-call input tokens) / sum(gap seconds)
```

A tool completion between items discards the pair because it starts another request.
Samples below 20 tokens or 0.5 seconds are excluded. Fewer than three qualifying
samples produce no rate. The effective tokenizer supplies estimated counts.
The estimate covers tool arguments, not message text or reasoning.
Only single-model turns support model attribution.

### Amp observed throughput estimate

`estimated_output_tokens_per_second` uses captured assistant content and live hooks:

```text
(estimated assistant text + thinking + tool-argument tokens)
/ (live-observed turn seconds − union of live-observed tool windows)
```

Only completed turns with matched `agent.start` and successful `agent.end` qualify.
Every tool needs terminal live call/result hooks. Tool windows must be ordered
and inside the turn. Replayed snapshots, late revisions, interrupted/running
turns, and nonpositive non-tool windows do not qualify.

Exclude user prompts, tool results, and inter-turn idle gaps.
Count revisions once per message ID. Count distinct messages separately, even
when text matches. Count captured thinking only, not hidden reasoning.

Aggregate rates divide summed tokens by summed non-tool seconds. They do not
average turn rates. If any included turn is ineligible, omit the aggregate;
eligible turns can still show their rates. Runtime aggregates cover the root
session, not overlapping subagents. Model usage covers the selected session's turns.

Counts use the effective tokenizer, normally `cl100k_base` as an Amp proxy,
with an offline fallback. Counts and live-timing provenance survive publication.
Old artifacts without provenance do not gain a fabricated estimate.

The denominator includes prefill, first-token latency, plugin overhead, and other
non-tool waiting. This is not provider-reported generation or pure decode speed.
Do not attribute it to a guessed model. Amp provider usage, billed cost,
`output_tokens_per_second`, and `decode_tokens_per_second` remain unavailable
without separate evidence.

Update matching readers, authority, and collectors before publishing new facts.
Preparation version changes invalidate disposable caches. Existing remote
artifacts require explicitly authorized republication to gain new source evidence.

## Visible tokens and images

Item measurements estimate visible input/output text with a tokenizer.
Inline base64 image bytes are replaced by an omission marker for sizing.
Only retained metadata, such as media type and dimensions, contributes to visible
tokens and context composition. This does not estimate provider image-token cost.

Previously prepared graphs retain stored measurements until source content or
the preparation version changes. A new reader alone does not rewrite artifacts.
