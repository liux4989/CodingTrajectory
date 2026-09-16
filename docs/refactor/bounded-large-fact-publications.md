# Bounded Large Fact Publications

- **Status:** Implemented locally; deployment qualification deferred
- **Wire impact:** Staging batch count ceiling only; fact rows and
  `ct.published_facts.v1` are unchanged

## Measured bounds

Privacy-safe owner-local evidence found one legitimate 10,602,862-byte graph in
a selected 29-graph cohort. None exceeded 16 MiB. The broader valid cohort had
graph p99 7,077,894 bytes and maximum 7,556,009 bytes. The graph bound is
therefore 16 MiB: 58% headroom over the measured legitimate maximum. The
512 KiB row bound is unchanged because measured maximum rows were far below it.

Complete-source publication is independently bounded at 96 MiB and 512 graphs.
The byte bound gives 41% headroom over the measured 71,239,324-byte maximum;
the cardinality bound gives 4.7× headroom over the measured 108 graphs. These
are storage/transaction workload limits, not Worker heap allowances. Revisit
16 MiB if a privacy-safe valid graph distribution approaches 12 MiB at p99 or
14 MiB maximum. Revisit 96 MiB/512 only with Durable Object transaction latency,
SQLite growth, and publication distributions showing sustained pressure above
80 MiB or 400 graphs.

## Memory and atomicity

Staging still uses one retryable protocol. A request contains at most 512 rows
and 2 MiB of canonical row JSON. The Worker verifies row hashes, privacy, and
number spelling before writing both the original batch and normalized staged SQL
rows. A changed batch invalidates its graph validation attestation.

Publication validates one graph at a time with SQL relationship checks. JS
materializes neither graph payload rows nor a publication aggregate. The only
graph-sized JS value is the canonical digest basis containing sorted
`(kind, fact_id, row_hash)` entries. The existing 131,072-row v1 cardinality
limit remains unchanged. The 16 MiB graph-byte limit is checked before SQL
constructs one digest-basis string, which is independently capped at 16 MiB;
the Worker therefore does not also hold a separate manifest and copied digest
string before WebCrypto's one-shot encoding. The final
synchronous transaction rechecks every attestation generation, enforces the
96 MiB sum, applies staged rows with set-based SQL, writes graph metadata one
graph at a time, closes replacements/tombstones, and advances one visibility
sequence. A late invalid graph therefore exposes none of the earlier graphs.

Read pages remain signed, snapshot-pinned, and bounded at 2,048 rows/1 MiB.
Local Wrangler qualification committed an exact 96 MiB synthetic publication
and read it through 107 bounded pages. Process RSS is not used as isolate-heap
evidence because local `workerd` includes SQLite and runtime state. The memory
proof is structural: a bounded staging request, a compact per-graph digest
manifest, scalar affected-graph projection, and no publication-wide payload
array. Deployment qualification must still confirm isolate telemetry and CPU
time under the production runtime.

## Compatibility

Canonical row bytes, row hashes, graph digests, public historical semantics,
replacement/tombstones, source fencing, publication sequencing, and
`ct.published_facts.v1` do not change. Existing invisible staged batches are
discarded once by an explicit transactionally versioned migration when
normalized staging is introduced; collectors also treat any batch missing its
normalized rows as absent and restage it through the existing retry flow.

Publication idempotency receipts are checked before staging validation and are
written in the same SQLite transaction as row visibility. An exact retry,
including after process restart, returns the original receipt without staging;
reuse of the key with a different request is a conflict. Process RSS remains
unsuitable as isolate-heap evidence. Production isolate telemetry and CPU time
remain mandatory deployment gates.
