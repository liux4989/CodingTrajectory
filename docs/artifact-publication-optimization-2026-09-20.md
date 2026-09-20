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
