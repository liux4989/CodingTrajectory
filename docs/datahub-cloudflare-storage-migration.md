# Datahub storage migration to Cloudflare

Date: 2026-09-10. Status: superseded for the current publishing task by the
[direct local snapshot](datahub-local-snapshot.md). D1/R2 migration is not required.

## Scope and recommendation

Move hosted Datahub's bounded Chronicle store to D1 and R2. Keep Cloudflare
Access and the private Python facade. This removes Supabase Auth, reader
passwords, PostgREST, and RLS from the hosted read path. The initial scope is
Datahub publication and reads; replacing the remaining collector control plane
is a separate scope decision. Local JSONL and SQLite remain local authority.

This proposal supersedes the Supabase storage choice in
[private hosting](datahub-cloudflare-private-hosting.md) only after a verified
cutover. That document still describes the deployed preview. The pending
Supabase reader-password rotation and deployment are paused.

## Storage responsibilities

| Component | Responsibility |
| --- | --- |
| D1 | Workspace publication head, immutable publication manifests, project/session indexes, artifact references, source epochs, idempotency records |
| R2 | Immutable, bounded, sanitized Chronicle payloads and large read projections, addressed by workspace and content digest |
| Private Python facade | Existing Pydantic validation, Chronicle replay, graph/tree/item handlers, revision-pinned reads |
| Gateway and Access | Owner browser admission, JWT verification, same-origin API boundary |
| Separate ingestion Worker | Authenticate publisher, enforce workspace and payload limits, validate publication, write R2 and D1 |

Large graph bodies must not be D1 row values. D1 currently limits a row/string/
BLOB to 2,000,000 bytes and a query or batch to 30 seconds. Use D1 for bounded,
indexed metadata and R2 for payload bytes. Python Workers can access D1 through
native bindings; retain the Python CT semantics instead of rewriting them in
TypeScript. Verify R2 byte conversion and decompression in workerd before live
publication.

The existing `HostedDatahubService` accepts an `AsyncRpcClient` protocol. A
Cloudflare adapter can initially implement the four existing read contracts:
`ct_workspace_snapshot`, `ct_project_inventory_snapshot`,
`ct_project_sessions_projection`, and `ct_historical_snapshot`. This is a
compatibility boundary for response semantics, not a port of PostgreSQL RPCs.
Preserve paging, revision cursors, source epochs, digest checks and graph closure.

## Publication consistency

1. Build the already-approved seven-day sanitized export, including graph
   closure. Validate schemas, workspace, uncompressed size and canonical digest
   before accepting it. Do not copy raw local evidence or the full database.
2. Write immutable R2 objects and verify successful completion and stored-byte
   checksums. Record canonical digest, encoding and encoded/decoded byte limits.
3. Stage bounded index rows under a publication identifier. Staged rows are
   invisible to reads and may be written in multiple bounded batches.
4. Validate completeness, then atomically finalize the manifest and advance the
   workspace head in one D1 transaction. Enforce the expected previous head and
   idempotency key inside that transaction; a stale publisher must fail rather
   than advance or partially commit. A zero-row conditional update must be
   treated as a conflict, not success.
5. Readers pin the committed publication once, then read only that publication's
   immutable indexes and R2 references. Never silently mix revisions or fall
   back to Supabase when a referenced object is missing.

D1 batch operations are transactional and R2 object operations are strongly
consistent, but there is no transaction spanning both products. Publishing
objects before the D1 visibility change makes failed uploads invisible. Garbage
collection must retain all objects referenced by committed, rollback, or active
staged publications and use a grace period for abandoned uploads. If D1 read
replication is enabled, establish a primary/session bookmark policy before
claiming read-after-write behavior.

## Authorization and privacy

Bind the facade to one configured workspace for this owner-only preview; reject
requests for other workspaces. D1 does not replace PostgreSQL RLS automatically:
every storage query and object lookup must enforce workspace scope explicitly.
The facade exposes only existing read routes; storage bindings are capabilities,
not proof that the code is read-only.

Keep R2 private, without a public bucket endpoint. Retain full-host Access and
gateway JWT validation. Use a separate narrow publisher credential and ingestion
audience for the local collector; browser access must not authorize publication.
Do not install a broad Cloudflare account token in the collector or application.

Retain the current bounded remote-data policy, including sanitized narrative
previews. Credentials, prompts, raw event bodies, command text and host paths
must not enter remote payloads or deployment receipts. API responses remain
`Cache-Control: no-store`.

## Implementation and cutover gates

1. Implement D1 schema, R2 encoding and publication validation with an isolated
   candidate store. Keep production authority explicit.
2. Add the Cloudflare read adapter and select it explicitly in the candidate
   facade. Replace Supabase-specific release gates for this backend with binding,
   publisher-policy and store-integrity checks.
3. Publish the bounded export; verify idempotent retry, concurrent publication
   conflict, interrupted upload, staged-index isolation, and missing/corrupt
   object failure with integration qualification. Do not add unit tests.
4. Compare all seven hosted route results at a pinned revision against the
   canonical local export: snapshot, changes, projects, sessions, graph, tree,
   and items. Require digest/replay and cursor parity, not just matching counts.
5. Run the metrics gate and full baselines if metric-sensitive code changes.
   Qualify actual workerd startup, first/concurrent requests and native bindings.
   Storage replacement alone does not prove the earlier Worker 1101 resolved.
6. Verify signed-out denial and authenticated owner JSON on the candidate.
   Record candidate Worker versions and store manifest; promote the gateway
   binding only after these checks pass.
7. Retain the prior Worker/store pair and rehearse rollback without mixing stores.
   Supabase deletion and remaining control-plane migration happen separately.

## Current evidence and platform references

The current adapter in
`packages/plugins/datahub/datahub_plugin/hosted/transport.py` signs in with a
dedicated Supabase reader and calls PostgREST; the service in the neighboring
`service.py` owns the four contract calls. Those are the initial replacement
points. No D1 database, R2 bucket, migration, or Cloudflare-backed runtime has
been created by this proposal.

- [D1 limits](https://developers.cloudflare.com/d1/platform/limits/)
- [D1 transactions and binding API](https://developers.cloudflare.com/d1/worker-api/d1-database/)
- [D1 from Python Workers](https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/)
- [R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/)
