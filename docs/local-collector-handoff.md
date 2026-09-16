# Local collector handoff

The collector publishes canonical historical facts derived on an authorized
host. It does not own source interpretation or historical semantics.

## Input and output

Production ingestion first preserves source occurrences and reconstructs the
canonical `DocumentStore`/`SessionGraph`, including exact measurements before
retention. `published_fact_set_for_store` produces bounded `PublishedFactSet`
values. The collector persists delivery/checkpoint state, stages those exact rows,
and submits an atomic publication manifest.

Raw provider records, occurrence inventories, transcript/tool bodies, source
paths, and credentials remain local. Source checkpoints contain only source
epoch/sequence, complete offsets, parser provenance, and digests. Published edge
and evidence references may retain safe canonical UUIDs.

## Remote calls

- `ct_project_register` and `ct_collector_register_source` establish portable
  identities.
- `ct_collector_publish_observation` commits metadata-only source checkpoints.
- `ct_collector_stage_fact_rows` stages bounded rows by graph digest and batch.
- `ct_collector_missing_fact_rows` resumes interrupted staging.
- `ct_collector_publish_facts` validates and commits complete graph revisions.
- `ct_collector_recover` returns source/publication watermarks.
- living heartbeat/change calls use their separate authority.

A scoped bearer token is sufficient; Cloudflare account credentials are never
installed on the collector host. Collection is project-scoped. An overlapping
graph requires its complete accepted source vector; partial scope fails closed.

## Recovery guarantees

The local delivery store retains exact pending requests, idempotency keys,
attempts, and receipts. A lost response retries the same request. Fresh local
state recovers remote source epochs and publication watermarks before assigning
new sequences. New fact-set digests may replace incomplete staging, while batches
for one digest must agree on count.

Exact replay derives the same row hashes and set digest. A failed stage or
publication exposes no partial graph. Successful replacement atomically reuses
unchanged rows, versions changed rows, and closes omitted rows/graphs within the
declared source scope.

The collector enforces the same publication limits documented in
[Chronicle history](chronicle-history.md). Exceeding a limit stops publication;
it never falls back to raw or weaker data.
