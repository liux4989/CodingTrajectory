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
