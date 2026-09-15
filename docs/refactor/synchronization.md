# Fact synchronization and recovery

## Publication sequence

```text
fence source occurrences
  → reconstruct canonical graphs and exact measurements
  → derive bounded PublishedFactSet values
  → publish metadata-only source checkpoints
  → stage immutable fact-row batches
  → atomically commit graph manifests and fact revisions
  → retain the publication receipt locally
```

The collector does not upload raw records, source occurrence inventories, or
tool bodies. A source checkpoint contains offsets/digests only. Each graph
manifest names a deterministic fact-set digest, counts, source IDs, and observed
time. Fact rows are staged separately and become visible only after the complete
source vector and every staged row pass validation in one workspace transaction.

## Bounds and paging

- one canonical fact row: 512 KiB encoded;
- one graph's fact set: 8 MiB encoded;
- one atomic publication: 16 MiB of staged encoded rows and 512 graphs;
- one fact read page: 2,048 rows and 1 MiB encoded, whichever is reached first.

These limits leave substantial headroom inside the Worker's 128 MiB isolate
limit while bounding parsed publication plans and read responses. Oversize work
fails before commit. Read cursors bind the pinned workspace sequence and the
normalized graph/session/project/vendor/time/kind selector, so they cannot be
reused across scopes.

## Retry and replacement

Staging is replaceable until publication. A new digest atomically supersedes old
batches for that agent/graph and may declare a different batch count. Batches for
the same digest must agree on count. Publication sequences and idempotency keys
make exact retries duplicate-safe; a changed request under an existing key is a
conflict.

Source epochs fence stale collectors. A publication must contain the accepted
checkpoint for every represented source and the complete source set for an
overlapping graph. A valid replacement closes prior row revisions, reuses
unchanged row hashes, inserts changed rows, and tombstones omitted graphs only
within the declared complete-source scope.

After a lost response, recover source and publication watermarks before assigning
new sequences, then retry the retained request. At-least-once attempts plus
idempotent commits provide duplicate-safe effects; network delivery is not
claimed to be exactly once.

Living heartbeats and changes remain separate from historical facts. Their
freshness never changes a pinned historical snapshot.
