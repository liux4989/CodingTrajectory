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
