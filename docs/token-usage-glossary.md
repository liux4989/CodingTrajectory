# Token usage glossary

Tokens are the units used to measure model input and output. A token can be a word,
part of a word, or punctuation.

Keep values reported by the AI provider separate from values calculated by this
project. Providers count totals differently: Pi includes cached input in
`totalTokens`, and Codex can report reasoning separately. A missing count means
unknown, not zero.

## Common terms

| Term | Meaning | Field | CLI label |
| --- | --- | --- | --- |
| Prompt | Input sent to the model; may include cached input | `prompt_tokens` | `prompt` |
| Fresh input | Input that is neither read from nor written to the cache | `uncached_prompt_tokens` | `fresh input` |
| Cached input | Input reused from the provider's cache | `cached_prompt_tokens` | `cached input` |
| Cache write | Input saved to the provider's cache | `cache_write_tokens` | `cache write` |
| Output | Tokens produced by the model, as reported by the source | `completion_tokens` | `output` |
| Reasoning | Thinking tokens reported separately by the source | `reasoning_tokens` | `reasoning` |
| Reported total | The original total from the provider or log | `reported_total_tokens` | `reported total` |
| Processed total | The project's total, adjusted to avoid counting tokens twice | `processed_tokens` | `processed total` |
| Prompt + output | The project's prompt count plus output; cached input depends on the provider's counting rules | `prompt_completion_tokens` | JSON only |

The processed total adds fresh input, cached input, cache writes, output, and
reasoning that is not already included in another count. Tokens already included
in a provider's count are not added again. The reported total stays unchanged.
Use `uncached_prompt_tokens` when you need fresh input only.
Compact CLI JSON may shorten `prompt_completion_tokens` to `prompt_completion`.

## How input and totals are counted

Codex input totals include cached input and cache writes. Fresh input is
`max(0, input_tokens - cached_input_tokens - cache_creation_input_tokens)`.
The reader converts `cache_write_input_tokens` to `cache_creation_input_tokens`;
public responses call this `cache_write_tokens`. This applies to both individual
response counts and running totals. If all input is cached, fresh input is zero.
A cache miss alone does not tell us how many tokens were written to the cache.

The CLI hides cache and reasoning counts when they are zero. When fresh input is
unknown, it shows the provider's `prompt` count instead; this may include cache.
Estimated-share columns use the order
`fresh input/cached input/cache write/output/reasoning`. Audit lines can also show
the reported total and the prompt-plus-output total including cached input.
The CLI calculates `prompt + output (including cache)` as:

```text
fresh input + cached input + cache writes + output
```

This differs from `prompt_completion_tokens`, whose cache inclusion depends on
the provider.

Claude usage includes every stream record with usage data, even when records
share `message.id`. Repeated usage records each contribute to the total, so this
measures recorded usage and may differ from billed usage. Assistant-response and
tool counts still follow their existing rules for identifying distinct items.
Session stats label category breakdowns as estimated shares and provider context
counts as input for the latest request. Latest-request input is different from
usage added up across the session.

## Costs and estimated shares

Reported cost comes directly from the provider. Estimated cost uses a price list.
Stats `cost_summary.total_cost` adds these request-level costs, preferring reported
evidence. `cost_summary.cache_savings` is always estimated: it compares list-price
cost with caching against the same token usage without cache discounts or cache
creation. It includes the write premium, so a write without enough later reads can
produce negative savings. `no_cache_cost` includes the unchanged output costs.
The comparison preserves each request's model and cache-inclusive input-length
pricing tier; it does not reprice an aggregate session at a larger tier. Missing
prices leave the affected total absent rather than producing a partial total.
`requests` and `priced_requests` expose total-cost pricing coverage.

Claude five-minute writes cost 1.25 times the input price; one-hour writes cost
twice the input price. Recorded duration breakdowns are preserved as
`cache_write_5m_tokens` and `cache_write_1h_tokens` (compact CLI JSON uses
`cache_write_5m` and `cache_write_1h`). They are subsets of `cache_write_tokens`,
not additional tokens. One-hour writes use their higher rate; writes without a
duration breakdown use the five-minute estimate.
Source: [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing#prompt-caching).

Calculate cost separately for each token type, since rates can differ.
Each request estimate uses that request's pricing tier and prompt size.
Turn, model, and session costs add up the request estimates; they do not choose
a new pricing tier from the combined token count.

`session.request_usage` lists usage for each request. `session.tool_usage`
estimates how that usage is shared among items and tools. Each usage record is
split among visible items in the same turn, in proportion to their visible token
counts. Later turns cannot change those shares. Each share uses the original
request's pricing tier, and the shares add up to that request's estimated cost.
These shares help explain usage; the provider's recorded totals remain the source
for total usage.

Requests and tool results may be linked because their timestamps fall in the same
time window. This does not prove the provider received that tool result. Whether
it was part of the input stays unknown unless the source confirms it.

## Model time

Session stats and usage expose execution time as `runtime.llm_seconds` and
`runtime.tool_seconds`. Tool time is the union of completed tool intervals,
clipped to each turn's boundaries, so parallel tools count once. LLM time is
the remaining turn time, including request overhead and waiting for output;
it estimates model activity rather than measuring provider inference directly.
Both fields are unavailable if any turn boundary or tool completion is missing
or invalid. Time waiting for the user between turns is separate.

Codex may record one tool call as both a generic response item and a native
lifecycle item. Timing joins those rows by call ID, using the earliest start
and latest recorded completion. Derived nested actions use their observed
wrapper's interval; their unknown individual outcomes do not invalidate the
wrapper's recorded timing.

The split covers all turns, including turns without recorded token usage.
`model_active_seconds` at session scope covers only turns with recorded usage
and remains the denominator for token throughput. Graph runtime shows the root
session's split; each subagent has its own session section. Execution totals
round each turn to whole seconds, while split fields retain millisecond precision.

`model_active_seconds` measures the recorded turn duration minus time spent
running tools. Overlapping tool runs are subtracted only once.
Claude uses a `turn_duration` record when available instead of waiting for the
next user prompt. Time spent waiting for the user between turns is excluded.

If a tool run has no recorded end, the turn cannot be used for this measurement.
If a turn uses several models, its full duration cannot be assigned to the most-used
model. A combined speed requires enough evidence to calculate the total model
time. Model time includes more than the time the provider spends generating tokens.

### Processing speed

`processed_tokens_per_second` is `processed_tokens / model_active_seconds`.
It excludes estimated tool-result tokens and tool costs.
A speed calculated from the full turn duration includes tool execution and must
use a different label.

### Output speed

`output_tokens_per_second` is `completion_tokens / model_active_seconds`.
The time includes processing the prompt and waiting for the first output token.
Codex includes reasoning in its output count. When a provider reports reasoning
separately, it is not added to the output count used here.

### Codex tool-argument generation speed (estimate)

`decode_tokens_per_second` estimates tool-argument generation speed from gaps between
completed response items in the same response:

```text
total tool-argument tokens / total gap seconds
```

If a tool finishes between two items, that pair is excluded because it starts
another request. Samples below 20 tokens or 0.5 seconds are excluded. At least
three qualifying samples are needed to show a speed. The selected tokenizer
estimates the token counts. This measures tool arguments only, excluding message
text and reasoning. A speed can be assigned to a model only for single-model turns.

### Amp output speed (estimate)

`estimated_output_tokens_per_second` uses saved assistant content and events
recorded during the live run:

```text
(estimated assistant text tokens + thinking tokens + tool-argument tokens)
/ (recorded turn seconds − time spent running tools, counting overlaps once)
```

Only completed turns with matching `agent.start` and successful `agent.end` events
qualify. Every tool needs live events for its call and final result. Tool start and
end times must be in order and within the turn. Replayed snapshots, late edits,
unfinished or interrupted turns, and turns with no positive time left after
subtracting tool runs are excluded.

Exclude user prompts, tool results, and time waiting between turns.
Count edited versions once per message ID. Count separate messages separately,
even when their text matches. Count only thinking saved in the log; hidden
reasoning is unknown.

Combined speed divides total tokens by total time excluding tool runs; it does
not average the speeds of individual turns. If any included turn lacks the
required evidence, omit the combined speed. Qualifying turns can still show their
own speeds. Combined runtime measurements cover the main session and exclude
overlapping subagents. Model usage covers the selected session's turns.

Counts use the selected tokenizer, normally `cl100k_base` to approximate Amp tokens,
with a fallback that works offline. Published results retain the counts and the
evidence for their live timing. Older saved results without that evidence cannot
be given an estimate.

The measured time includes prompt processing, waiting for the first token, plugin
work, and other waiting outside tool runs. This estimate differs from
provider-reported generation speed and token generation alone. Assign it to a
model only when the source identifies that model. Amp provider usage, billed cost,
`output_tokens_per_second`, and `decode_tokens_per_second` remain unavailable
without separate evidence.

Before publishing new measurements, update the readers, code that determines
which evidence to trust, and collectors together. Changing the preparation
version makes existing reusable caches out of date. Adding new source evidence to
existing remote results requires explicitly authorized republication.

## Visible tokens and images

Visible tokens are estimates of the input and output text we can read, counted
with a tokenizer. For sizing, inline base64 image data is replaced by a marker
saying the image was omitted. Only kept details, such as image type and dimensions,
contribute to visible token counts and context breakdowns. These counts do not
estimate the provider's image-token cost.

Saved graphs keep their existing measurements until the source content or
preparation version changes. Updating a reader alone does not rewrite saved results.
