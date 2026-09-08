# Chronicle Non-production Reset and Canary — 2026-09-09

- **Status:** Completed
- **Scope:** CT application objects in the explicitly authorized disposable
  non-production project
- **Data handling:** Aggregate evidence only; identifiers, credentials, payload
  bodies, source paths, and artifact digests are omitted

## Target and preflight

The collector profile and linked Supabase configuration resolved to the same
active, healthy project. The existing authenticated owner, workspace,
collector, and capabilities were verified before mutation.

| Check | Observed result |
| --- | ---: |
| CT tables before reset | 25 |
| CT rows before reset | 7,971 |
| Unrelated public tables | 0 |
| External foreign-key dependencies | 0 |
| External view dependencies | 0 |
| Committed migrations parsed | 11 |

## Reset and rebuild

The reset ran as one CT-only database transaction. It removed the 25 CT tables
and 7,971 application rows, rebuilt the schema from all 11 committed migrations,
and restored the workspace owner, collector registration, and four collector
capabilities. The Supabase authentication account was preserved.

After commit, the application contained 25 CT tables and no source observations
or artifact revisions. The remote migration ledger exactly matched the local
chain. `supabase db push --dry-run --linked` reported that the linked database
was up to date.

## Canary publication

The collector selected one bounded, complete Codex graph and published it with
an ordinary authenticated credential.

| Check | Observed result |
| --- | ---: |
| Complete graphs | 1 |
| Source checkpoints accepted | 1 |
| Chronicle artifacts accepted | 1 |
| Sessions | 1 |
| Turns | 2 |
| Items | 20 |
| Canonical artifact bytes | 22,591 |
| Failed, rejected, or pending deliveries | 0 |
| Incomplete graph scope | 0 |

The resulting artifact uses `ct.chronicle_graph.v1`. No raw transcript, prompt,
command, tool-output, or event body was uploaded.

## Integrity and protocol checks

The post-publication database audit found exactly one current artifact and one
revision. Its stored digest matches the canonical payload hash. Its single
source-vector entry joins to the accepted source checkpoint, and its resource
index contains one session, two turns, and 20 items. Four required Chronicle
functions are installed and no retired Shareable function remains.

The exact publication request was replayed with its original idempotency key. It
returned the original receipt and committed sequence without creating another
revision. A distinct request using the already committed sequence returned
`stale_publication_sequence` and did not change the artifact snapshot. A
deliberately retired `ct.shareable_graph.v1` request was rejected and likewise
created no revision.

## Authenticated remote reads

A fresh client used the ordinary authenticated token from an empty directory,
with local discovery replaced by a failure sentinel. It verified the sole
artifact's identity, digest, Chronicle schema, body-free coverage, and lookup by
session, turn, and item resource indexes.

The following methods passed their response contracts at one pinned remote
snapshot: `project.sessions`, `session.overview`, `session.summary`,
`session.tree`, `graph.overview`, `session.stats`, `graph.stats`,
`session.usage`, `graph.usage`, `session.model_usage`,
`session.request_usage`, `session.tool_usage`, and metadata-only
`session.items`.

`session.events`, `session.search`, and `session.items` with
`include_content=true` each failed closed as local-only evidence methods. No
service-role credential was used for publication or read verification.

## Qualification boundary

The authorized reset and Chronicle canary are complete for this disposable
non-production target. The result does not authorize destructive reconciliation
of any retained or production database. A separate agent-host installation,
continuous collection, and living or estimation workload qualification were not
part of this run.
