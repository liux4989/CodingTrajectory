# Local Collector Handoff

- **Status:** Compact v2 collector implemented; deployment and private validation pending
- **Owner:** Local agents with access to representative vendor logs and runtimes
- **Depends on:** Applying the committed Supabase migrations and provisioning a scoped agent credential

## Purpose

The local collector is the only component that reads host vendor logs. It turns
local source changes into deterministic normalized observations, persists them
before delivery, publishes them idempotently, and maintains liveness leases. It
does not answer shared historical queries and does not publish SQLite caches.

## Required inputs from the control plane

The committed ingress migration provides:

1. Agent registration and scoped credentials.
2. Project registration and private location registration.
3. Source registration returning stable `source_id` and current epoch.
4. Observation ingestion with durable idempotency receipts.
5. Source-epoch rollover for truncation or replacement.
6. Lease heartbeat and living-observation endpoints.
7. Strict versioned request and receipt models in
   `coding_trajectory.control_plane.collector_protocol`.

The collector uses the RPC names `ct_collector_register_source`,
`ct_collector_publish_observation`, and `ct_collector_heartbeat` through the
Supabase REST RPC endpoint. The caller needs an authenticated principal that
matches the registered agent and has `ingest` plus `living` capability; the
service role is never placed in the collector environment.

## Collector responsibilities

```text
discover source
  -> identify source and epoch
  -> read only committed complete records
  -> normalize with existing Python vendor adapter
  -> apply measurements retention and remote-boundary scrubber
  -> assign deterministic event identity and source sequence
  -> write durable outbox record
  -> publish with stable idempotency key
  -> retain receipt and advance local acknowledged watermark
```

The collector must reuse existing Codex, Claude Code, and Pi adapters rather
than create remote-only parsers. It may retain source paths and byte offsets in
its private state. Shared payloads use canonical IDs and portable project IDs.
The collector sends a compact `canonical_session_snapshot.v2` payload assembled
by those adapters, not raw JSONL records. It applies the existing measurements
retention mode followed by a remote-boundary scrubber before the payload enters
the outbox. That compact payload excludes tool inputs/outputs, commands, full
assistant/reasoning text, unbounded event payloads, item vendor data,
context-source text, host paths, and file/media-like bodies. The host `cwd`,
Codex `cwd`, and Pi session-file fields are stripped before publication; the
source path never leaves the SQLite state. Upgrading a v1 collector advances
each registered source to a fresh epoch; pending local v1 payloads are retired
rather than published under the compact authority.

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
9. The compact wire payload contains no tool body, command body, host path,
   file/media body, data URI, or base64 body.
10. Remote `session.events` and `session.items` return retained compact evidence
    through the shared API and declare their scope.

## Operation

Inspect eligible sources without emitting paths or reading transcript bodies:

```sh
uv run ct collector scan --global-scope
```

After applying the migrations and registering a project and agent, provide only
local credentials and identifiers, then run one collection pass:

```sh
export CT_SUPABASE_URL=https://your-project.supabase.co
export CT_SUPABASE_ANON_KEY=...
export CT_COLLECTOR_ACCESS_TOKEN=...
uv run ct collector run --global-scope --since-days 7 \
  --workspace-id <workspace-uuid> --agent-id <agent-uuid> --project-id <project-uuid>
```

### Refreshable macOS credential profile

For a recurring local collector, store the Auth user's password only in macOS
Keychain. The profile file is mode `0600`, contains no JWT or password, and
holds the workspace/agent identity plus the project URL and publishable key.
Each collection pass signs in just-in-time for a fresh user JWT.

```sh
uv run ct collector credentials configure \
  --profile default \
  --workspace-id <workspace-uuid> --agent-id <agent-uuid> \
  --supabase-url https://your-project.supabase.co \
  --supabase-api-key <publishable-key> \
  --email <collector-auth-email>

uv run ct collector run --credential-profile default --global-scope --since-days 7
```

Use `ct collector credentials status --profile default` to confirm only that
the profile and its Keychain password exist. It never prints a password, JWT,
API key, email address, or identifiers.

The default outbox is private local state at
`~/.coding-trajectory/control-plane/collector.sqlite3`. `ct collector status`
reports only its pending count. Re-running `run` preserves the exact queued
payload and idempotency key; a rejected record stays inspectable locally and
does not prevent another source from publishing.
