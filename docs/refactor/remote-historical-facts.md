# Greenfield remote historical-support refactor: Published Fact Sets

Status: implemented in this branch. This document is the authoritative design
record for the clean-break replacement of the artifact/chunk/projection remote
historical stack with typed, bounded, versioned **publication facts** stored in
Durable Object SQLite.

## Decision record (owner-approved, no compatibility)

- The old remote implementation is **replaced completely**. No migration,
  dual-write, dual-read, legacy routes, old-schema preservation, feature flags,
  or adapters. Frozen historical contracts are revised/versioned where the
  accepted design requires it; the protocol baseline is regenerated
  deliberately (`scripts/check-core-protocol.py --update`).
- No compatibility aliases anywhere: graph methods require `root_session_id`;
  session methods require `session_id`; `turn_id` is a subordinate filter only.
- `num_turns`/`drop_turns` are replaced by deterministic pagination:
  `session.overview` uses `before_turn_id` + `limit`; `session.items` and
  `session.events` use `cursor` + `limit`.
- The protocol keeps one absolute `modified_since`/`since` timestamp; the CLI
  translates `--since-days`/`--days` locally. `since_days`/`days`/`period_days`
  leave the protocol.
- `session.events` arbitrary payload filters are replaced by typed
  type/status/tool-name/ID filters (there are no payloads to filter).
- One stable response shape per method: all response-composing include flags
  are removed (`include_content`, `include_actors`, `include_metrics`,
  `include_narrative`, `include_serialized_context`,
  `include_interruptions`, project.sessions `include_*`/`usage_*` variants).
  Trimmed evidence is expressed with nullable fields plus explicit coverage.
- Bounded metrics child collections are always-on with deterministic caps and
  `coverage.trimmed` honesty (evaluated against paging: rollup collections are
  small and sorted; caps are documented per method).
- Ordinary HTTP `Content-Encoding` remains allowed as transport optimization
  only. No stored whole-artifact gzip, no base64 payload protocol, no
  compression field in any fact contract.

## Architecture

1. **Local provider adapters** still build complete canonical sessions from
   immutable provider logs. Local raw logs remain the raw authority.
2. **Publication processing** derives one bounded typed `PublishedFactSet`
   from the private Chronicle (`ct.chronicle_graph.v3`). Chronicle stays the
   full private bounded representation (previews retained); it is **not**
   redefined as body-free. Standard local and remote historical APIs consume
   the same `PublishedFactSet` representation, so local and remote standard
   reads are identical by construction.
3. The **collector** stages typed, bounded fact-row batches
   (`ct_collector_stage_fact_rows`). Cloudflare validates IDs, uniqueness,
   parent/reference integrity, cardinalities, field limits, row hashes, and the
   deterministic fact-set digest, then commits one workspace publication
   sequence atomically in Durable Object SQLite
   (`ct_collector_publish_facts`, one `transactionSync`).
4. The store keeps queryable, versioned fact rows —
   graph/session/turn/item/event/edge/request/model/runtime/measurement/
   output-evidence — with stable IDs,
   parent/order indexes, `valid_from_sequence`/`valid_to_sequence`, row hashes,
   and atomic replacement/tombstones. Unchanged rows are reused
   (content-addressed by row hash).
5. One internal **`FactRepository`** contract. Local uses an in-memory
   published fact set derived from live discovery; remote fetches selected SQL
   fact pages (`ct_fact_read`). Shared Python historical handlers own
   summary/overview/search/metrics/display semantics. Cloudflare owns
   authorization, pinned sequence, filtering, paging, and row retrieval only —
   no display/metric semantics in TypeScript.
6. **Living** remains a separately versioned authority (`living.*` unchanged).
   Remote living publication applies a bounded remote profile at the collector
   boundary: inline user/assistant text is truncated to bounded previews with
   original `size_chars`, and item shapes are view-bounded (oversized values
   become `content_ref`, which is not dereferenceable remotely). Raw tool
   bodies are never uploaded by default.

## Bounded evidence contract

- Standard historical methods have one `facts` behavior locally and remotely;
  no source-dependent richer default. The former local evidence branching
  (`LocalEvidenceRepository`, `include_content`, full-payload `session.events`)
  is removed. No local raw-body diagnostic surface is added (explicitly
  optional and omitted for this refactor).
- Persisted per fact: structural identity/topology/order/timestamps/status,
  item↔event IDs, orchestration edges/evidence, portable paths, canonical tool
  concepts, native numeric measurements/accounting, provenance.
- **`ToolOutputEvidence`** is deterministic, owned once by the canonical item
  (`item.output_evidence`), referenced from event envelopes. Fields:
  lifecycle/status/outcome/exit code/duration, exact character measurement,
  token measurement with `method`/`tokenizer`/`provider` provenance,
  truncation state, allowlisted structured `facts`, optional bounded redacted
  `preview` (declared allowlisted processors only), `processor` +
  `processor_version` + `source_event_ids` provenance, and explicit coverage.
- Unknown tools fail closed to `facts_only`. There are no generic arbitrary
  output previews. Sanitized content is never labelled `complete` relative to
  raw evidence.
- Coverage distinguishes `not_applicable`, `not_retained`, `preview`,
  `complete`, plus searchable completeness (`complete`/`preview`/`facts_only`/
  `none`).
- Never uploaded: raw tool input/output, command stdout/stderr, patch/file
  bodies, full prompts/transcripts/reasoning, raw event payloads, arbitrary
  vendor_data, blobs/media, secrets, host-absolute paths.

## Contract versioning (no aliases)

- `ct.chronicle_graph.v3`: adds bounded event envelopes
  (`coverage.events=True`) and per-item `output_evidence`.
- `session.summary` v2, `session.overview` v3, `session.items` v4,
  `session.events` v4, `session.search` v2, `graph.overview` v3.
- Unchanged (exact from retained facts): `session.tree` v2, `session.stats` v1,
  `session.usage` v1, `session.model_usage` v1, `session.request_usage` v1,
  `session.tool_usage` v1, `project.list` v2, `project.sessions` v3,
  `project.overview` v2, `living.*`.
- Envelope `content_scope` becomes `"facts"`.
- `validation/core-protocol.json` is regenerated from the intentional source
  changes; Loop generated types are regenerated from it.

## Remote authority and durability

- Durable Object SQLite is the new remote authority. Tables:
  `fact_rows` (`graph_id`,`kind`,`fact_id`,`valid_from_sequence`,
  `valid_to_sequence`,`row_hash`,`payload`), plus versioned graph publication,
  collector, living, and checkpoint state.
- Exact enforcement limits are 512 KiB per canonical row, 8 MiB per graph,
  16 MiB of staged rows per atomic publication, and 1 MiB/2,048 rows per read
  page. Read cursors bind the pinned snapshot and normalized selector/kinds
  scope. These aggregate limits bound Worker materialization under its 128 MiB
  isolate memory limit.
- Local provider logs can republish/rebuild the remote authority at any time:
  rerunning the collector re-derives identical fact sets (deterministic IDs
  and hashes) and replays publication sequences. There is no migration and no
  independent artifact backup. **Operational consequence:** if local provider
  logs are deleted before republication, remote history for them is not
  recoverable; the remote store is a derived, rebuildable projection, not a
  backup of raw logs.
- Recovery (`ct_collector_recover`) reports per-source checkpoint watermarks,
  the next publication sequence, and accepted fact-set digests so a collector
  can skip unchanged republication.

## Deleted without compatibility

- Whole Chronicle gzip artifact creation/storage/download and the base64
  decode path (`ct_historical_artifacts`, `_decode_artifact`,
  `ct_collector_stage_artifact_payload`).
- Chunk DAG/descriptors/packs/manifest reconstruction (`upload_chunks.py`,
  `upload.ts`, `canonical_blocks`, `ct_collector_upload_chunks`,
  `ct_collector_missing_chunks`, `ct_collector_stage_chunk_manifest`).
- Sparse compact wire artifact as a remote read authority
  (`catalog_protocol.py` snapshot projections, `read_projections.py`).
- Project-session/canonical/resource projection duplication
  (`build_read_projections`, `build_resource_projections`,
  `resource-projections.ts`, `published_catalog`, catalog selection/cursor
  tables, `ct_catalog_migrate`, `ct_projection_capabilities`).
- `CloudflareHistoricalRepository` artifact reconstruction/cache and
  `LocalEvidenceRepository` branching (`local_evidence.py`,
  `_requires_local_evidence`).
- Managed-collection machinery that exists only for those paths:
  `canonical_repository.py`, `upload_service.py`, `upload_state.py`,
  `upload_capture.py`, `publication_lock.py`, `collector_service.py`,
  `ct collector sync/service`, and their qualification scripts.
- Cloudflare estimator RPC/routes/storage (`estimation.ts`,
  `ct_estimate_*`, `ct_estimator_*`, `estimate`/`estimate_worker` roles) and
  the R2 `ARTIFACTS` binding. Public Core keeps 18 methods; no `estimate.*`.

## Source qualification

An independent local replay of the private OSO source pair against reviewed
ingestion commit `1f3e86cab69c931a2580fa0ec6de00f71ad99ba4` passed with unchanged
source/evidence hashes. Both trajectory and measurement projections matched
independently derived processed totals: 6,706,498 child tokens and 17,453,805
graph tokens, excluding the prior 240,402 inherited-token inflation. The replay
also observed 2 sessions, 3 completed graph turns, a spawned edge with verified
parent turn/item/event origin, all 476 parent and 319 child occurrences with
independently checked digests, and a 24-segment parent union delivered to 11
children.

One copied, untagged metadata occurrence after the child's owned start remains
in the filtered stream. Its occurrence is preserved and it has no canonical
event, accounting, or structural effect. This receipt closes the real-OSO gap
only for that exact source pair and ingestion commit; it does not prove
universally complete ownership classification. No raw source evidence was
transferred or changed during qualification.

## Scale evidence

`scripts/benchmark-fact-publication.py` publishes synthetic facts to local
workerd, including a near-16-MiB publication whose largest row is near the
512-KiB boundary. It records stage/publish/read timing, largest row, maximum
encoded page, and page count. These local single-run measurements are
engineering evidence, not a production latency SLO. Machine-readable output is
written to the ignored
`.artifacts/fact-publication-benchmark.json`. The companion
`scripts/qualify-cloudflare-control-plane.py` validates integrity failures,
version reuse/closure, staging replacement, tombstones, snapshot/scope-bound
cursors, historical parity, secret/body denial, aggregate byte rejection,
byte-bounded reads, authentication, and living separation.
