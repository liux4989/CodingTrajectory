# Receipt-based publication: local qualification

Implementation baseline: handoff commit
[`6253caf`](https://github.com/liux4989/CodingTrajectory/commit/6253cafda713f09c6d83694e374a222a2f4258c5),
tree `e9c0c03af8b7baa1b73e81c28ceadc81ca6db156`, not the orb's original
`origin/main`. The supplied bundle hash, prerequisite, advertised ref/tree, both
evidence archive hashes, and their internal SHA256SUMS were verified.

All runtime work used disposable local Miniflare. No Cloudflare calls,
deployment, remote publication retry, credential access, or remote resource
changes occurred. Package installation and Git prerequisite fetching were the
only setup network activity. Independent Mac diagnosis patch comparison remains
pending transfer; none of its source or measurements is claimed as inspected.

## Ownership and invariants

- Upload authenticates, checks the input SHA/schema/bounds, and validates index
  headers, exact/page mode, child-reference bounds, contiguous pack coverage,
  sizes and posting positions. It establishes a pending claim before touching
  R2. Completion is recorded only after PUT success or GET verification of an
  existing object's size, workspace/kind/hash metadata, and complete digest.
  Existing immutable objects are not rewritten.
- Claims retain their original `(kind, sha256)` key, adding a generation token
  and nullable completion JSON containing workspace, bytes, and index facts.
  Duplicate claims extend expiry without downgrading completion. Expired claims
  get a new generation. Completion only updates a matching unexpired generation;
  it cannot insert a missing claim or resurrect one released at commit.
- Publication resolves claims in keyed batches of 50 and compares manifest
  identities, unique methods, child references and bytes. Source checkpoints,
  graph identities, publication sequences, transaction/receipt behavior, and
  complete-inventory requirements remain enforced. Already retained objects
  remain reusable. Validated index header/reference facts live in existing
  `api_methods` descriptors for the retained view lifetime, with a hash expression
  index for bounded reuse lookups; no object-body cache or new wire format exists.
- The summary GET, digest and semantic checks remain because publication needs
  session cards. Inventory construction and cleanup remain unchanged. All
  serving SHA, header, size, response and read-budget checks remain unchanged.
- One workspace invocation promise queue orders source writes, upload claims and
  completions, publication preparation/transaction, and cleanup. It replaces the
  publication's long-held DO input gate. A rejection does not poison the queue.
  This is local serialization, not a durable job system or guaranteed completion
  after client cancellation. Existing idempotency/recovery handles unknown
  outcomes. Replacement's separate input gate was not changed.

## Exact-fixture publication measurements

The transferred near-budget fixture SHA256 is
`8ec30be77b2bc928d752fb73e9cb566679329e07d31874a2cf52fa288c08edeb`:
1,007 objects, 497 indexed methods, full paths and original API coverage.
Both variants used identical local instrumentation and fixture bytes. Three
fresh runs were interleaved before/after without concurrent benchmark processes.
Timing begins after upload and ends after publication response, including its
existing cleanup; it is not overview timing, Cloudflare CPU, or remote latency.

| Publication operation | Handoff source | Candidate |
| --- | ---: | ---: |
| R2 GET | 498 | 1 |
| R2 HEAD | 1,007 | 0 |
| Inventory PUT | 6 | 6 |
| Cleanup LIST | 2 | 2 |
| Total R2 calls | 1,513 | 9 |
| SQL exec calls | 566 | 589 |
| SQL cursor rows read | 2,063 | 4,075 |
| SQL cursor rows written | 2,536 | 3,036 |
| SQL logical changes | 1,520 | 1,520 |
| Local wall milliseconds, runs 1/2/3 | 811.830 / 803.604 / 824.052 | 97.653 / 93.758 / 95.936 |
| Local wall median milliseconds | 811.830 | 95.936 |

Removed: 497 repeated index GET/hash/parse validations plus 1,007 HEADs, not
merely their serial scheduling. Added: 23 keyed claim queries reading 2,012
SQLite cursor rows, plus 500 expression-index writes on existing method inserts.
Post-commit database size in these disposable runs was 724,992 → 991,232 bytes.
Upload now adds one completion RPC/SQL update per object; it uses GET instead of
HEAD so an existing object can be verified fully. These upload costs are not
included in publication timing. The fixture, caps and metric expectations did
not change.

The unrelated-claim qualification seeds 1,000 pending claims, re-completes only
the summary, and republishes from retained objects. Near-budget claim lookups
use 23 calls and read 22 cursor rows, not 1,000 rows per reference. Cleanup's
existing workspace-wide claim enumeration remains a separate operation.

Linux x64, Node v26.8.2, Miniflare 5.20260907.0-alpha,
workerd 1.20260907.1. Cursor counters include SQLite index activity, not billed
Cloudflare metrics. Inventory bytes were 3,439 in each near-budget timed run.

## The remaining awaits still require removing the input gate

Using the exact representative fixture and the same ledger implementation,
injecting 4,500 ms before every publication R2 operation gives:

- Counterfactual publication input gate: 30,010.153 ms, 503, then confirmed
  `artifact_snapshot_unavailable` (no manifest commit).
- Ordered invocation without that gate: 36,078.183 ms, successful commit,
  1 GET + 6 PUT + 1 LIST, followed by the full API qualification passing.

Thus the ledger alone does not remove the 30-second input-gate failure mode.
The candidate does not increase any timeout or add verification concurrency.
These controlled local results do not establish the first boundary responsible
for historical staging timeouts or explain the earlier Amp runner stall.

## Executable qualification

`scripts/qualify-prepared-api.mjs FIXTURE --publication REPORT` adds disposable
receipt/race qualification to the existing end-to-end API checks. Covered:
old-schema claim migration; missing/pending/expired completions; interrupted and
failed PUTs; exact existing-object reuse without PUT; size/workspace/kind/hash
and same-size digest mismatches; malformed index semantics; source, method,
dependency and byte mismatches; release/replay; retained-index reuse; generation
fencing; pending/completed cleanup protection and expiry; duplicate-upload/commit
and cleanup/upload races; concurrent publications and source checkpoint order;
and a rejected invocation followed by successful work. No unit tests were added.

Reproduce timing independently of adversarial setup:

```sh
# In an untouched handoff worktree with the same instrumentation scripts:
node scripts/qualify-prepared-api.mjs NEAR_FIXTURE --publication-baseline before.json
# In the candidate worktree:
node scripts/qualify-prepared-api.mjs NEAR_FIXTURE --publication-timing after.json
node scripts/qualify-prepared-api.mjs REPRESENTATIVE --publication-timing gated.json 4500 --input-gate
node scripts/qualify-prepared-api.mjs REPRESENTATIVE --publication-timing ordered.json 4500
```

Passed local checks: `npm run check --prefix cloudflare/control-plane`,
`scripts/check-metrics-quality-gate.sh`, direct
`uv run python scripts/validate-metrics-baselines.py` (all four baselines),
`uv run python scripts/qualify-prepared-api.py` (real producer),
`uv run python scripts/qualify-collector-preparation.py` (30 checks), and
`uv run python scripts/qualify-connection-workflows.py` (15 checks).
The exact transferred fixtures also pass the full prepared-API qualification.
`scripts/qualify-prepared-api-staging.mjs` also passed against disposable local
Miniflare: successor 1,008 HTTP / 1,007 upload requests, combined 1,078 HTTP,
8 preflight reads and all 602 full-workload reads. This exercises the existing
resume driver locally; it is not a remote staging continuation.
The older `benchmark-artifact-publication.mjs` emits v1 facts/summary and lacks
v2 API objects; it is not evidence for this candidate. Its cleanup scenarios are
covered in the v2 executable qualification instead of weakening upload schemas.

## Deployment implications, not deployment instructions

Nothing is deployed. Previously uploaded, unretained objects with old claims
must re-complete through authenticated existing-object verification before
publication. Old retained views remain readable, but their indexes need one
upload-boundary verification before republishing because they lack stored index
facts. Neither case requires rewriting a verified R2 object. No such remote
verification has been performed or authorized here.

The added SQL columns are a forward migration. Old source uses positional
three-column claim inserts, so rolling back to it after migration is not a safe
upload rollback. Review that boundary before any deployment; no compatibility
framework or production data deletion is included. Staging remains untouched.

## Follow-up: structural manifest deduplication

The subsequent real-project inventory exceeded ingress and documented SQLite
row bounds. Manifest v3 changes the wire and stored representation, not the
prepared object payloads or the complete-inventory publication model. Each graph
replaces `api_objects` and `api_methods` with four explicit `api` tables:

| Table | Row |
| --- | --- |
| `objects` | `[sha256, bytes]`; kind is always `api` |
| `methods` | `[method, method_version]` |
| `scopes` | Full session/graph scope string |
| `entries` | `[method_position, scope_position, turn_id, object_position]` |

Positions are zero-based integers. A null object position represents the existing
`remote_result_too_large` descriptor; zero is a valid object position, not an
error. Turn IDs remain explicit, including null. Tables are per graph; entries
and objects retain their original order. Expansion restores explicit kind,
index, error and turn fields before existing publication checks and view hashing.
Full paths, content, identities and prepared-object hashes remain unchanged.
There is no compression, truncation, larger ingress cap or publication batching.

New collectors send v3, and new publications store/return v3. The Worker still
accepts v2 requests; updated Python readers and retention/cleanup code understand
both stored forms. Local staged requests remain in their original expanded form.
Before uploads, the collector measures the exact compact HTTP body, retaining the
3 MiB bound and unchanged outbox on rejection. Recovered publication receipts
settle retries without replaying an old committed request in a new wire encoding.
The transaction rejects stored manifests above 2 MiB minus 4 KiB row headroom.
Update Worker and readers before running the updated collector. Old readers and
old cleanup code do not understand newly stored v3 manifests: do not roll back
to them after v3 publication without a separately reviewed migration.

Local-only measurement of the exact private 175-source/102-graph inventory:

| Representation | Before | Compact |
| --- | ---: | ---: |
| RPC envelope | 3,429,365 B | 1,537,087 B |
| Normalized stored manifest | 3,502,541 B | 1,484,404 B |

The stored figure is a deterministic projection, not a Cloudflare measurement.
It leaves 608,652 B below the guarded row limit. Full normalized equality held
for every graph and stored manifest, including all 15,112 API objects, 7,278
methods, 7,202 indexed methods and 76 existing size errors. Those method size
errors are unchanged and are not solved by manifest deduplication.

The executable prepared-API qualifier covers Python/TypeScript table parity,
invalid positions, duplicate objects, compact commit/read, legacy retention,
atomic oversized-row rejection and the existing receipt/cleanup races and API
reads. Collector qualification covers exact UTF-8 ingress boundaries, rejection
before uploads, and old-receipt recovery without replay. These checks are local;
this follow-up does not deploy, retry publication or alter staging data.

## Follow-up: oversized prepared results

Local replay of the preserved corpus isolated all 76 errors: 32 event indexes
(71,307–558,042 B) and 10 item indexes (66,390–160,472 B) exceeded the 64 KiB
index bound. No individual event/item record, topology or pack failed.
The other errors were 27 complete tool-usage responses (20 session, 7 turn)
and 7 complete session request-usage responses, exceeding the 440 KiB result
bound. The largest complete tool response was 2,721,779 B.

Large item/event indexes now use `mode: page_columns`: their small root retains
sizes and pack references, while `posting_objects` references separate immutable
lookup columns (`id`, `item_id`, `turn_id`, `types`, `status`, `tool_name`).
Only columns selected by request filters are fetched. Upload validation checks
column semantics and publication retains every dependency. Unfiltered reads
still need at most root + base + two packs (4 objects); filtered reads may use
up to 10 objects. The 768 KiB total fetched-byte limit, 64 KiB root limit and
448 KiB response/object limit remain. Item/event packs target 128 KiB to leave
room for metadata; individually larger existing records still have the original
256 KiB pack allowance. Selection budgets actual pack bytes before fetching.

`session.request_usage` and `session.tool_usage` are v5 paged methods, accepting
`limit` (default 200, maximum 1000) and a signed `cursor`. Aggregate counts,
usage, costs, warnings and policy remain complete and repeat on each page.
Append `requests`, or independently append `tool_items` and
`item_real_token_costs`, until `next_cursor` is null. Tool arrays are paired only
by position internally: their membership, order and unequal lengths are kept,
not joined or deduplicated by item ID. `total`/`returned` count these detail
positions (the maximum of the two array lengths), not aggregate tool count.
`tool_item_count` keeps its original meaning. Pagination reserves space for
cursor, missing IDs and page metadata before selecting rows.

Preparation v4 invalidates cached v3 API views. Canonical facts, metric builders
and their expected values are unchanged. Existing v3 manifests remain readable,
but usage v4 descriptors cannot satisfy v5 requests. Reprepare and republish to
make those views available; deployment alone does not repair old descriptors.
The collector's existing obsolete-outbox guard stops old pending preparations
without sending them. Preserve the old database/receipts and use fresh disposable
collector state after reconciliation, rather than rewriting an uncertain outbox.
No rollout or remote reconciliation is performed by this local change.

The executable prepared-API qualifier adds `--shape index-heavy` for large
indexes and asymmetric usage collections. It checks full ordered reconstruction,
canonical aggregate equality, filters, sparse IDs, pagination/cursor binding,
malformed column uploads, missing dependencies and maximum-size usage pages.
Finite object and fetch bounds remain intentional; this does not promise
unlimited graph sizes or silently truncate oversized future records.

The private local replay with the final reader prepared all 76 formerly failing
methods and passed 764 scenarios / 10,441 reader calls, including `limit=1000`.
Maximum measured fetch was 694,050 B / 9 reads; maximum response was 446,619 B
(limit 450,560 B), root 34,138 B and posting object 298,443 B. Full array order
and noncollection totals matched the preserved inputs. The original database
hash remained unchanged. This is Python corpus evidence, not a deployed Worker
measurement. The local Worker index-heavy publication qualification separately
passed column/filter parity, maximum usage pages and receipt/cleanup adversarial
checks. Normal producer qualification, collector (30), connection (15), npm
check, Ruff and all four direct metric baselines also passed.

The private verifier subsequently completed all 102 graphs / 175 sources from
preserved facts in 105.848 seconds: 7,278 methods, 18,640 API object references,
and zero error descriptors. All 2,428 usage inputs matched array order and
noncollection totals across 2,619 pages at limit 200; all 34 saved canonical
usage inputs also matched the current producer bases. Full-corpus maximum root
was 64,295 B, posting object 298,443 B, pack 262,140 B and references/graph 1,822.

The exact sorted-object-order collector RPC is 1,799,761 B, leaving 1,345,967 B
under the unchanged 3 MiB ingress limit (SHA256
`dc36e7336d408fa6ed6e9cad1c5f72747eaaef53c2995cbab54633893b6c9d97`).
Projected compact storage is 1,747,078 B, leaving 345,978 B under the 2,093,056 B
row guard (SHA256
`fc9f4d67954597e8d8b65105ab90a3f624910bd6aab75ecf87e4e4a62ee1f1fd`).
The storage projection uses snapshot 999 and published_at
`2026-09-20T09:30:00.000Z`; both compact graph expansions matched their originals.
These are local corpus measurements, not remote runtime certification. Network
was blocked and the original database, cache, outbox and accepted publication
were untouched. No private scratch or session content was transferred.
