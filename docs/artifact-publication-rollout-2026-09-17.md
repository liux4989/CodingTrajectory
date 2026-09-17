# Immutable artifact publication rollout, 2026-09-17

Status: implemented and locally qualified; **not deployed**. This is an
approval-ready internal rollout plan, not authorization to change Cloudflare or
upload private data.

## Product boundary

The collector remains the compute authority. It still parses the complete
available inventory, constructs canonical session graphs, performs fact-model
validation, computes metrics, and prepares `project.sessions` cards. The change
does not make facts smaller and does not redesign the parsers. It avoids repeated
work by caching each graph preparation under the hash of its canonical graph,
then stores immutable fact and summary objects in R2.

The Worker/Durable Object is intentionally thin:

- one authenticated collector owns publication for a project;
- uploads are workspace-keyed, content-addressed, bounded, schema-checked, and
  idempotent; each upload records a seven-day workspace claim before touching R2;
- publication verifies every novel R2 object and every accepted source
  checkpoint, reuses exact `(kind, sha256, bytes)` attestations from retained
  manifests, then commits one complete manifest atomically;
- readers fetch prepared summaries for lists and only the selected graph's facts
  for details; graph-root, session, turn, and item IDs are routing aliases;
- the shared reader cache is bounded to 16 entries and 32 MiB, and materialized
  graphs live only inside those bounded indexes;
- each project retains its latest three manifests. The first publisher marker is
  retained to distinguish pre-migration snapshots from expired artifact
  snapshots; publisher history, retry receipts, and orphaned R2 objects are
  pruned to the rollback window.

This deliberately gives up arbitrary artifact history and multi-publisher merge
or conflict machinery. A snapshot before a project's first artifact publication
uses the existing SQL reader. A snapshot older than the retained artifact window
returns `artifact_snapshot_expired`; it must not silently show unrelated legacy
facts. Existing SQL facts are not deleted and remain the rollback source for the
pre-cutover snapshot.

## Complete inventory and failures

A manifest means **complete inventory for one project at one accepted source
vector**. A previously registered file missing beneath an existing readable
parent is a deletion and may publish a smaller (including empty) inventory. An
absent source parent, a malformed/changing source, or an incomplete checkpoint
is unavailable input: collection records a failure and leaves the last completed
manifest visible. The first publication cannot infer an empty inventory; an
empty manifest is allowed only after a prior completed publication establishes
the project boundary.

Uploads are invisible until the manifest transaction commits. An interruption
leaves only content-addressed orphans. A retry first recovers a committed
receipt; without one it refreshes claims and uploads the complete manifest,
while the same idempotency key recovers the same receipt. Cleanup runs after
commit under the workspace Durable Object concurrency barrier, protects all
retained references and unexpired upload claims, and is safe to repeat. Each
publication scans at most four
1,000-object R2 pages and persists its continuation cursor, so later successful
publications finish large orphan sets without an unbounded request. A cleanup
failure or a run of interrupted publications affects quota until a later
successful publication retries cleanup, but does not affect visibility.

Normal changed-graph runs upload only object hashes absent from the collector's
last acknowledged manifest. The Durable Object HEAD-checks novel or expired
references and trusts exact retained references under the early-internal
invariant that this Worker and its serialized cleanup are the only R2 mutators.
An unsupported direct caller that waits more than seven days between upload and
publication must re-upload to refresh its claim first.

## Local qualification

Run against disposable local Wrangler state only:

```sh
cd cloudflare/control-plane
npm run check

# In another terminal, with the repository's synthetic local principal registry:
npx wrangler dev --local --port 8794 \
  --persist-to /tmp/ct-artifact-qualification \
  --var "CT_PRINCIPALS:$(cat /tmp/ct-principals.json)" \
  --var "CT_CURSOR_KEY:local-qualification-cursor-key-00000001"

cd ../..
PYTHONPATH=packages/core/src uv run python scripts/qualify-artifact-publication.py
node scripts/benchmark-artifact-publication.mjs /tmp/artifact-publication.json
```

The qualification covers authorization and malformed uploads, interrupted
publication, absent-receipt complete retry, committed-receipt recovery, initial
and unchanged collection, changed-only uploads, graph merge/split, deletion,
unavailable roots, prepared list/detail reads, root and child aliases, retained
and expired snapshots, and cache entry/byte eviction. The
benchmark records local workerd SQL cursor counters, HTTP and instrumented R2
operations, retained bytes, and wall time. macOS runs additionally sample the
shared workerd process CPU and RSS; these are neither isolate measurements nor
Cloudflare billing counters and cannot guarantee Free-plan capacity.

## Approval-ready rollout

1. Select and record the exact candidate commit. Review the deployed-version to
   candidate diff, generated contracts, R2 binding, and the local/Mac evidence.
2. Back up the complete principal registry and confirm the existing production
   `ARTIFACTS` binding. Deploy code first. Do not reset the Durable Object, delete
   SQL facts, recreate the bucket, or run a data migration.
3. Verify version, identity, workspace isolation, reader-only denial of upload,
   collector denial of read without `read`, and legacy reads at a pinned
   pre-cutover snapshot.
4. With explicit approval for the real data scope, run one manual collector for
   one project. The first run uploads its full current complete inventory and the
   atomic manifest becomes that project's cutover. Stop on unavailable sources,
   unexpected object size/count, conflict, or a partial/mismatched read.
5. Pin the new snapshot. Compare `project.sessions`, one graph-root detail, and
   one child/item-routed detail with local canonical output. Record request/R2
   counts and retained bytes; do not extrapolate a guaranteed quota capacity.
6. Exercise an identical retry and one bounded changed-graph run. Keep schedules
   paused until both succeed. Roll forward to fix publication failures; rolling
   Worker code back does not roll data back. Readers can use a retained manifest
   explicitly, while the pre-cutover SQL snapshot remains non-destructively
   available.

Known limitations: the prototype still parses the whole inventory before graph
reuse; R2 cleanup is publication-triggered and requires a later changed
publication to resume, so inactive workspaces have no bound on abandoned-object
age; seven-day expiry was tested with disposable timestamp control rather than a
seven-day wall-clock wait; retention is exactly three completed artifact
snapshots; an out-of-band R2 mutator violates the retained-attestation invariant;
and operation measurements are local workerd evidence, not production load or
billing evidence.
