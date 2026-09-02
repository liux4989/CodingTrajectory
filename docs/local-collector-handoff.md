# Local Collector Handoff

- **Status:** Deferred implementation contract
- **Owner:** Local agents with access to representative vendor logs and runtimes
- **Depends on:** Remote control-plane ingestion contracts and authenticated API

## Purpose

The local collector is the only component that reads host vendor logs. It turns
local source changes into deterministic normalized observations, persists them
before delivery, publishes them idempotently, and maintains liveness leases. It
does not answer shared historical queries and does not publish SQLite caches.

## Required inputs from the control plane

Before collector implementation begins, the remote service must provide:

1. Agent registration and scoped credentials.
2. Project registration and private location registration.
3. Source registration returning stable `source_id` and current epoch.
4. Observation ingestion with durable idempotency receipts.
5. Source-epoch rollover for truncation or replacement.
6. Lease heartbeat and living-observation endpoints.
7. Request and observation JSON Schemas discoverable without reading logs.

## Collector responsibilities

```text
discover source
  -> identify source and epoch
  -> read only committed complete records
  -> normalize with existing Python vendor adapter
  -> assign deterministic event identity and source sequence
  -> write durable outbox record
  -> publish with stable idempotency key
  -> retain receipt and advance local acknowledged watermark
```

The collector must reuse existing Codex, Claude Code, and Pi adapters rather
than create remote-only parsers. It may retain source paths and byte offsets in
its private state. Shared payloads use canonical IDs and portable project IDs.

## Durable local state

Collector state may use SQLite, but its schema is delivery state only:

```text
registered_sources
  source_id, vendor, native_session_id, source_epoch
  committed_offset, next_source_sequence, last_digest

observation_outbox
  idempotency_key, source_id, source_epoch, source_sequence
  event_id, content_sha256, payload, state, attempts, last_error

remote_receipts
  idempotency_key, receipt_id, outcome, committed_sequence, received_at
```

Crashes between local write and network acknowledgement must cause an identical
retry, not a new event identity. A rejected record remains inspectable and does
not block unrelated sources.

## Data-collection validation

Local agents must validate with representative real sources without committing
private logs or secrets:

- one Codex, Claude Code, and Pi source;
- append during an active turn;
- process restart and exact retry;
- source truncation/replacement and epoch rollover;
- parent plus subagent sources arriving out of order;
- a session that resumes after a terminal observation;
- temporary/unmapped project followed by explicit project registration;
- network loss through outbox recovery;
- heartbeat delay and lease expiry.

Validation artifacts committed to the repository must be sanitized fixtures or
hashes and arithmetic reconstructed from source evidence. Real source paths,
prompts, credentials, and proprietary content remain local.

## Definition of done

1. No shared API reads directly from collector SQLite or vendor logs.
2. Duplicate delivery creates one accepted observation.
3. Conflicting identity reuse returns a durable conflict receipt.
4. A sequence gap does not advance the server's contiguous source watermark.
5. Truncation starts a new source epoch without deleting prior evidence.
6. Collector restart resumes every pending outbox record.
7. Lease expiry is reported remotely as `unknown`.
8. The same accepted observations produce the same graph hash when replayed.
