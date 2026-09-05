# Amp Local Collector

CodingTrajectory includes a project Amp plugin at
`.amp/plugins/coding-trajectory/index.ts`. It is the first transport for Amp
threads and establishes the host-local raw input for canonical ingestion.
Hosted collection receives only metadata checkpoints and the locally assembled
[shareable artifact](shareable-history.md), never these raw transcripts.

## Storage

The plugin writes one append-only JSONL source per Amp thread under:

```text
~/.coding-trajectory/amp/sessions/T-<thread-id>.jsonl
```

Set `CT_AMP_LOG_DIR` to override the root. Directories and newly created files
are private to the current user, subject to the process umask.

Each source contains three versioned record types:

- `thread`: thread ID, title, parent thread ID, workspace URI, and executor;
- `message`: Amp's stable plugin-facing message representation, including text,
  thinking, tool calls, and tool results;
- `observation`: a live hook observation for `session.start`, `agent.start`,
  `agent.end`, `tool.call`, or `tool.result`.

Version 1 observation records contain `captured_at`, `observed_at`, `thread_id`,
and `event`. Agent observations also contain `message_id`; agent-end and tool
result observations contain `status`; and tool observations contain
`tool_use_id` plus `tool_name`, `input`, `output`, or `error` when Amp makes
those fields available. This detail remains in the private local journal and is
not a shareable artifact by itself.

The collector reads `thread.messages({ full: true })`, paging through the whole
transcript so compaction does not discard earlier messages. It appends only new
or changed revisions of a thread or message record. Consumers must therefore
use the last record for a given thread or message ID.

Transcript reconciliation runs when the plugin loads, when a thread becomes
active, at session start, and at agent turn start/end. Tool hooks append their
observations promptly without scanning the full transcript for every tool
event. Writes are serialized because one Amp plugin process can observe several
concurrent threads. Durable agent and tool observations are deduplicated by
their event plus stable message or tool-use identity when those IDs are
available.

## Orb behavior

The plugin runs inside an orb and writes to that orb's local filesystem. Files
are not synchronized across orbs. The existing CT collector discovers these
journals as vendor `amp`, builds bounded shareable artifacts, and uses its normal
authenticated outbox/publication path. Never upload raw journal records.

Remote acceptance requires migration
`20260905010000_ct_shareable_graph_amp_vendor.sql`; it adds `amp` to the vendor
allowlist without changing the other validation rules. Apply it only to an
authorized target. This implementation does not provision collector credentials,
start a supervised uploader, or apply a database migration automatically.

## Discovery and deterministic relationships

Local calls can select Amp with:

```sh
uv run ct api call project.sessions --params '{"agent_vendor":"amp"}'
```

The adapter accepts only the versioned plugin journal, not `amp threads export`.
It replaces repeated message revisions, matches tool calls/results by tool-use
ID, and recognizes only successful `create_thread` results with an explicit
valid `threadID` and a matched live call/result observation pair as creation
evidence. Replay-only calls are not sufficient: replay can include internal
activity without reliable thread ownership. References from reads, messages, or waits
do not become parent edges. Conflicting claims and cycles are not accepted.

Parent-side spawn origins survive body-free publication. Captured children are
connected when both sessions are available to the same graph assembly, including
incremental rebuilds seeded by the child. Missing children remain references,
not fabricated sessions. Separately published cross-orb artifacts are not merged
by the current remote authority; deployment across collectors must not claim
complete cross-host graph coverage.

Offline integration acceptance (no network writes):

```sh
uv run python scripts/validate-amp-live.py
```

## Current fidelity

The stable Amp plugin transcript includes prompts, assistant text and thinking,
tool inputs/results, and message IDs. Parent lookup was observed to return null;
creation-tool evidence is used instead. The transcript does not expose
provider inference timestamps, token usage, cost, or complete model-routing
metadata. Those fields require a separate enrichment source and must not be
inferred from the collector records.

`observed_at` is the time the plugin hook arrived (captured before asynchronous
write queuing), not the time provider inference or tool execution began.
`captured_at` has the same hook-arrival value for live observations and records
when a transcript snapshot was requested for thread/message records. Replayed
or reconciled transcript content therefore must not be assigned an inferred
historical event time. The collector uses only the current plugin thread/message
schema and does not invoke a CLI historical export.

Public session/graph responses carry `measurement_coverage` for Amp, describing
observation timestamps, explicit-creation-only relationships, and estimated
content tokens. Usage dictionaries report `availability: unavailable` instead of
zero provider consumption. Recorded usage-request counts are not an inference
request count. Bound duration forecasts, calibration, and retrieval exclude Amp
observation times from execution ground truth; task-text prediction remains
available. No exact model inference duration, cache accounting, or billed cost
is reconstructed from message lengths.

## Linux orb credentials and retry

A successful local scan proves discovery only. `401 JWT expired` blocks remote
validation; it does not indicate an Amp ingestion failure. `CT_ACCESS_TOKEN` is
an API credential; collector runs use `CT_COLLECTOR_ACCESS_TOKEN` and require a
workspace and a provisioned collector agent. Copying an expired token between
variables cannot repair authentication.

For repeated runs on a Linux VPS/orb, configure a refreshable profile using an
injected password environment variable. An administrator must first provision a
scoped Auth principal and collector agent with the required capabilities on the
authorized target. Never install a service-role key on the collector. Inject
`CT_COLLECTOR_PASSWORD` using the host's secret manager; do not put its value in
shell commands, repository files, or reports.

With the non-secret connection and identity variables populated, configure from
the project checkout on the orb:

```sh
uv run ct collector credentials configure --profile orb \
  --supabase-url "$CT_SUPABASE_URL" \
  --supabase-api-key "$CT_SUPABASE_ANON_KEY" \
  --workspace-id "$CT_REMOTE_WORKSPACE_ID" \
  --agent-id "$CT_COLLECTOR_AGENT_ID" \
  --email "$CT_COLLECTOR_EMAIL" \
  --password-env CT_COLLECTOR_PASSWORD
export CT_CREDENTIAL_PROFILE=orb
uv run ct collector credentials status --profile orb
uv run ct collector scan --agent-vendor amp --since-days 7
CT_AUTO_PUBLISH=0 uv run ct api call project.list
```

Configuration persists only the environment variable name alongside profile
settings in a private profile file. The password must be injected into each
process that refreshes the profile. Status reports secret presence only; the API
read above verifies authentication without triggering on-demand publication.
An explicit profile obtains a fresh access token even if `CT_ACCESS_TOKEN` is
still set to an expired value. Authentication alone does not prove collector
capabilities or that the required Amp migration is installed.

After read validation and target authorization, publish from the same project:

```sh
uv run ct collector run --credential-profile orb \
  --project-name "$CT_PROJECT_NAME" --agent-vendor amp --since-days 7 \
  --state-path "$CT_COLLECTOR_STATE"
```

Use a stable private state path for this collector stream. The profile supplies
workspace/agent identity; without a profile, collector runs also accept
`CT_REMOTE_WORKSPACE_ID` and `CT_COLLECTOR_AGENT_ID`. Refresh occurs at command
startup, not continuously during a long-running command. A revoked password or
missing injected secret fails closed; reprovision through the administrator.
