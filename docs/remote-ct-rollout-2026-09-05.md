# Non-production reset and seven-day publication

- **Date:** 2026-09-05
- **Status:** Completed for the CodingTrajectory project
- **Scope:** Current-project sources modified within seven days; one fenced snapshot
- **Authorization:** The user confirmed the configured target was non-production and authorized the CT reset.

## Reset

The collector profile matched the linked Supabase project and authenticated
successfully. Direct database access failed before authentication. A temporary
HTTPS CONNECT relay restored access, with the database hostname and Supabase CA
verified. No proxy configuration was changed.

Preflight found 24 CT tables, 186 CT rows, no unrelated public tables, and no
external foreign-key or view dependencies. One transaction removed the old CT
objects, rebuilt the current schema, and restored collector enrollment. The
Supabase authentication account was preserved. The rebuilt application has 25
CT tables; the schema history now includes 11 migrations after the publication
execution-budget adjustment below.

## Publication evidence

| Check | Observed result |
| --- | --- |
| Selected physical source files | 20 |
| Logical sources / sessions after resumed-segment coalescing | 18 |
| Accepted source checkpoints | 18 |
| Published graphs | 8 |
| Atomic publication bytes | 4,971,224 |
| Largest artifact bytes | 2,033,298 |
| Rejected or pending deliveries at completion | 0 |
| Artifact replay mismatches before publication | 0 |
| Remote artifact identity/digest mismatches | 0 |

Collection used an explicit seven-day filter and project scope. The collector
fenced complete source prefixes; appends after those fences belong to a later
run. Raw source logs were not modified or uploaded. Titles and prose previews
are omitted from the shared artifact.

The existing credential profile now references the rebuilt project. A fresh
private delivery database holds the accepted receipts and retry state. The
pre-existing default delivery database was preserved; subsequent collection
must use the verified fresh state explicitly rather than replaying an old queue.

## Publication timeout diagnosis

The first publication hit SQLSTATE `57014`: the authenticated API role had an
eight-second statement timeout. The request remained pending with its original
bytes and idempotency key. No graph revision became visible on failure.

Measured separately, request hashing took 5.96 seconds and the largest graph's
validation took 6.28 seconds. The complete authenticated publication transaction
passed all checks in 48.59 seconds during a measurement that was rolled back.

The publication RPC now has a bounded 60-second statement timeout; its client
allows 90 seconds for execution and transport. Other role/API timeouts remain
unchanged. This uses the documented [function-level timeout mechanism](https://supabase.com/docs/guides/database/postgres/timeouts#function-level).
All size, digest, schema, topology, ownership, and idempotency checks remain
enforced. The retained request then committed successfully through PostgREST.
This measurement establishes the observed corpus's behavior, not a throughput
guarantee for every publication at the 16 MiB limit.

## Authenticated read verification

An isolated client used an ordinary authenticated token, with local discovery
disabled and an empty working directory. It fetched all eight artifacts from
Supabase and verified their identities, digests, and strict artifact schemas.

The following shared methods passed response-schema validation at one pinned
snapshot: `project.sessions`, `session.overview`, `session.summary`,
`session.tree`, `graph.overview`, `session.stats`, `graph.stats`, `session.usage`,
`graph.usage`, `session.model_usage`, `session.request_usage`,
`session.tool_usage`, and metadata-only `session.items`. Per-resource method
checks used a representative graph; project collection covered all eight.
Project inventory discovery also passed.

`session.events`, `session.search`, and contentful `session.items` each returned
an explicit local-only rejection. No service-role credential was used for
publication or read verification.

The current client can read the uploaded snapshot remotely using authorized
workspace credentials. Installing/configuring a separate agent host, enabling
continuous collection, and verifying estimation or living-event workloads were
not part of this completed run.

## Local checks

The four committed metric baselines passed without changing expected values.
Modified Python lint/format checks and the diff whitespace check passed. The
metrics path gate was invoked and classified the execution-budget change as
outside its trigger paths; the full baseline workflow was run directly anyway.
