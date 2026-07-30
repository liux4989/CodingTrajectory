# Token Usage Glossary

CodingTrajectory keeps provider-reported totals separate from derived totals.
Providers do not agree on what their own totals include: Pi includes cached
prompt tokens in `totalTokens`, while Codex CLI token-count records can report
reasoning tokens separately from the provider total.

## Buckets

- `prompt_tokens`: fresh prompt/input tokens for the observation.
- `cached_prompt_tokens`: prompt tokens read from provider cache.
- `cache_write_tokens`: prompt tokens written into provider cache.
- `completion_tokens`: visible model output tokens.
- `reasoning_tokens`: model reasoning/thinking tokens when the source exposes
  them separately.

## Totals

- `reported_total_tokens`: provider/log-source total, preserved unchanged when
  the source reports one.
- `processed_tokens`: unified processed-token total after normalizing whether
  the source reports prompt tokens inclusive of cache or cache as separate
  additive buckets.
- `prompt_completion_tokens`: prompt plus visible completion:
  `prompt + completion`.

Cost should be computed from component buckets or provider-reported cost, not
from one total multiplied by one rate.

## Request pricing

`session.request_usage` is the pricing authority. Each provider request is
priced from its own prompt/cache/output buckets, including any pricing tier
selected by that request's prompt size. Turn, model, and session costs sum
those request estimates; they never apply a high-context threshold to an
already-aggregated turn.

## Common model throughput

`processed_tokens_per_second` is the canonical processed-token total divided
by `model_active_seconds`:

```text
(uncached prompt + cached prompt + cache write + completion + reasoning)
/ model-active seconds
```

`model_active_seconds` is derived from the turn boundary after subtracting the
union of completed, observed tool intervals. It is a model-throughput
denominator, not provider decoder-busy time. Turns with an unclosed tool
interval are ineligible, and mixed-model turns do not assign their duration to
the dominant model. A graph or session rate remains available when its full
turn denominator is reconstructable.

This metric intentionally excludes tool output tokens and tool monetary cost
from its scope. A full turn-duration rate may still be useful diagnostically,
but it must not be labeled as the common model-throughput rate because tool
execution time remains in that denominator.

## Allocated item cost

`session.tool_usage` exposes `item_real_token_costs` as a derived attribution
layer. The raw item chronology remains unchanged; each observed provider usage
record is allocated across the items visible in the same turn at that
observation, weighted by visible item tokens. The turn boundary makes prior
attribution stable as later turns arrive. The allocated rows reconcile in glossary buckets
(`prompt_tokens`, `cached_prompt_tokens`, `processed_tokens`, and related
fields), not in internal model field names such as `input_tokens` or
`total_tokens`. Item `estimated_cost` prices each allocated slice using the
source request's pricing tier; summing all item rows in a turn therefore
reconciles to the request-ledger cost for that turn.
