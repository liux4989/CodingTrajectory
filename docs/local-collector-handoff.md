# Local Collector Handoff

- **Status:** Manual seven-day publication and authenticated reads verified; supervision not enabled
- **Owner:** A host with authorized access to local vendor logs
- **Depends on:** The shareable-graph migration and a capability-scoped collector
  principal

## Purpose

The collector is the publication component for local vendor logs. Local evidence
loading can separately read bodies for already-published sessions.
It fences complete source bytes, builds one body-free shareable artifact, stores
delivery work durably, publishes project artifacts idempotently, and maintains
the existing living sequence. Local SQLite is delivery state, never remote
historical authority.

## Collection sequence

```text
discover project-scoped sources
  -> record one complete-line byte fence per physical segment
  -> derive fork trimming from those same fenced parent bytes
  -> coalesce resumed segments into one logical source/session
  -> build and validate ct.shareable_graph.v1 locally
  -> queue metadata-only source checkpoints
  -> obtain accepted checkpoint receipts
  -> assemble the complete collected graphs locally
  -> recover the agent/project publication watermark after pending retries
  -> queue one atomic artifact publication with a complete collected source vector
  -> publish with the original bytes and idempotency key
  -> heartbeat on the shared living sequence
```

Measurement extraction and artifact normalization consume the same in-memory
records read from the fence. Bytes appended after the fence are deferred to the
next pass. Parent fork-cut inputs are derived only from fenced records; the
collector never rescans an unfenced parent during normalization.

## Required remote contract

The collector uses:

- `ct_project_register` for portable project identity;
- `ct_collector_register_source` for stable logical sources and epochs;
- `ct_collector_publish_observation` for metadata-only checkpoints;
- `ct_collector_publish_artifacts` for direct atomic graph publication;
- `ct_collector_recover` for authenticated source, publication, and living watermarks;
- `ct_collector_heartbeat` for leases; and
- `ct_collector_publish_living_observation` for canonical living changes.

The collector principal needs only its scoped authenticated capabilities. A
service-role credential must never be installed on the collector host.

Remote collection requires a portable project name and project ID. The
repository identity and aliases are optional portable identifiers. A host path
is never project identity. Remote collection is project-scoped; `--global-scope`
is rejected. The default collection window is seven days.

## Durable local state

```text
registered_sources
  physical segment path, file identity, fence, logical source, epoch

logical_sources
  vendor/native session identity, source ID, epoch, next sequence, last digest

observation_outbox
  exact checkpoint request, idempotency key, attempts, outcome

artifact_outbox
  exact project publication, agent/project sequence, digest, attempts, outcome

living_outbox
  heartbeat and living changes on one monotonic observation sequence

remote_receipts
  durable accepted/duplicate/rejected/conflict evidence
```

Paths exist only in private collector state. They are not serialized into a
checkpoint or artifact.

On restart, in-flight records return to pending. A lost response retries the
byte-identical stored request with the same idempotency key. A pending or
unclassified rejected publication blocks assignment of a newer publication
sequence. A confirmed stale-sequence conflict is retained as superseded and
reconciled. An incomplete-graph rejection consumes its server sequence and is
retained as `rejected_scope`; it does not block a later expanded scan. Source
failures prevent a partial collected graph from being queued.

A fresh SQLite database recovers the agent's existing source epochs and accepted
checkpoint digests instead of assuming source sequence zero. It recovers the
publication watermark before assigning work and the living watermark before its
first heartbeat. Recovery never changes an uncertain pending request. Preserve
separate state per agent and run one collector per agent/project stream.

Time/vendor filtering preserves all unrelated remote artifacts. Replacing an
overlapping graph requires its complete previously published source set; a
partial scan cannot silently truncate the graph. The run result reports
`artifact_scope_incomplete` and a content-free remedy. Expand the scan only within
the authorized collection scope. Different agents can publish disjoint graphs
into one project; overlapping ownership fails closed.

## Artifact content

The collector publishes the structural/numeric core and constrained semantic
labels documented in
[`shareable-history.md`](shareable-history.md).
It never uploads raw logs, complete sessions, event arrays, commands, tool
inputs, tool outputs, prose previews, titles, or pending-plan text. A value
visible only inside a tool output therefore does not enter remote history.

The per-source checkpoint payload contains only:

```text
kind = ct.source_checkpoint.v1
complete offsets for the logical source segments
digest of the locally built source artifact
```

The remote graph payload exists only in the atomic artifact publication, not in
every source observation.

## On-demand queries

Local session queries can invoke this same collector with `target_session_id`.
The collector fences the selected source component, normalizes required fork
inputs, and queues only the requested canonical graph. Matching published
artifact digests skip publication. CLI batch and on-demand writers share an
agent lock; the on-demand caller uses durable private retry state, verifies
visibility of the requested artifact, and returns only a Supabase read.
See [fresh-session queries](cli.md#fresh-session-queries) for configuration,
scope, and explicit read-only behavior.

## Operational use

Before a run, confirm that the target is authorized and non-production. Keep
the API URL, publishable key, and collector access token in the local secret
environment or the existing Keychain-backed credential profile. Never put
secret values in command history, reports, or repository files.

Run with a portable project name. The collector registers that name when
needed, checks an explicitly supplied project ID for consistency, and then
uses the seven-day project-scoped default. `ct collector status` exposes only
the aggregate pending count.

## Definition of done

1. One resumed-session group creates one logical source registration.
2. Source epoch and sequence advance without collision or loss.
3. Fork trimming, local discovery, and the artifact retain identical turn and
   item identities.
4. Source checkpoints contain no graph or transcript body.
5. Every artifact source is present exactly once in the normalized source
   vector, and every vector source belongs to an artifact.
6. A partial or failed collection publishes no project artifact.
7. Exact replay produces the same artifact bytes and digest.
8. Lost-response retry reuses the exact request and idempotency key.
9. Artifact and publication size bounds fail closed.
10. Heartbeats and living changes retain one monotonic agent-instance sequence.
11. Fresh collector state resumes committed source, publication, and living sequences.
12. Filtered scans preserve history outside their scope; incomplete overlapping
    graphs are rejected without blocking a later complete scan.
13. Two agents publish disjoint graphs to one project without replacing each other.

## Verified non-production run

The 2026-09-05 run accepted 18 logical sources from 20 physical files and
published eight graphs, with zero pending or rejected deliveries. Its private
fresh delivery state must be used explicitly when resuming this publication
stream; the previous default delivery database was preserved. The publication
RPC has a 60-second database execution budget, with a 90-second client wait.
See [`remote-ct-rollout-2026-09-05.md`](remote-ct-rollout-2026-09-05.md).
