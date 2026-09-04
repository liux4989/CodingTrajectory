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

Each source contains two versioned record types:

- `thread`: thread ID, title, parent thread ID, workspace URI, and executor;
- `message`: Amp's stable plugin-facing message representation, including text,
  thinking, tool calls, and tool results.

The collector reads `thread.messages({ full: true })`, paging through the whole
transcript so compaction does not discard earlier messages. It appends only new
or changed revisions of a thread or message record. Consumers must therefore
use the last record for a given thread or message ID.

Capture runs when the plugin loads, when a thread becomes active, at session
start, and at agent turn start/end. Writes are serialized because one Amp plugin
process can observe several concurrent threads.

## Orb behavior

The plugin runs inside an orb and writes to that orb's local filesystem. This is
deliberately local-first: files are not synchronized across orbs. The future
hosted transport should send the same versioned records to an authenticated,
idempotent ingestion endpoint while retaining local JSONL as its retry journal.

## Current fidelity

The stable Amp plugin transcript includes prompts, assistant text and thinking,
tool inputs/results, message IDs, and parent thread identity. It does not expose
provider inference timestamps, token usage, cost, or complete model-routing
metadata. Those fields require a separate enrichment source and must not be
inferred from the collector records.
