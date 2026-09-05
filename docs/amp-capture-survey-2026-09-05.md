# Amp capture survey — 2026-09-05

## Scope

Read-only historical acquisition survey and a separate medium-mode live plugin
probe. No CT publication, global plugin installation, or production rollout is
part of this survey. Raw transcripts are not committed or uploaded.

Official references:

- https://ampcode.com/docs/orbs/customizing
- https://ampcode.com/docs/customize/plugins
- https://ampcode.com/docs/plugin-api

The installed CLI's `amp threads export --help`, `amp threads raw --help`,
`amp threads usage --help`, and `amp plugins show-docs` were also inspected.

## Historical acquisition: observed

`amp threads export <thread-ID>` successfully returns structured JSON in this
orb. `amp threads raw` is explicitly internal-user-only and is not a proposed
dependency. Export payloads were piped into Python for structural inspection,
not printed or saved as raw evidence.

| Sample | Exported messages | Observation |
| --- | ---: | --- |
| Prior upload validation, `T-01a06e1b-3d97-7364-adf8-062033fc463d` | 39 | 31 tool uses and 31 results; model/token usage fields present |
| Older remote-ledger work, `T-01a060be-bcfc-73fe-b3b4-41dbbf5e713f` | 849 | Matches Amp search's message count; four compaction summaries |
| Its inventory child, `T-01a06202-1b83-7069-ab06-ad4cccb022c5` | 68 | Separately exportable; no explicit parent/origin fields at root or in metadata |

The older sample contains:

- 428 user-role, 417 assistant-role, and four info-role messages. User-role
  messages include tool results; these counts are not human request counts.
- 598 tool uses and 598 tool results, with zero unmatched IDs in either direction.
- Unique `messageId` values for all 849 messages.
- Four summary blocks at message positions 125, 368, 503, and 731 (zero-based),
  with earlier messages still present and the first message containing text.
- 416 usage records; one assistant message has no usage. All 416 satisfy
  `totalInputTokens = inputTokens + cacheReadInputTokens + cacheCreationInputTokens`.
- Usage fields include model, timestamp, output tokens, context limit, and cache
  token breakdowns. This is observed field behavior, not a universal billing contract.
- Only 46 messages have `meta.sentAt`; no tool-use block has both `startTime`
  and `finalTime`. Do not claim complete event timing from this export.
- Tool results include `run.status`, with both done and error states observed.
- Four `create_thread` calls and three `Task` calls. This does not establish
  availability of every internal subagent transcript.

Amp search independently identified four children and their spawning tool IDs.
That search is evidence for this survey, not yet a collector discovery API.
Parent/child linkage through public collector interfaces remains to be mapped.

Two successive exports of the inactive 849-message thread produced identical
canonical message-array SHA-256 digests:
`4e3efe302a1179b7f6a086a8cdd7f220979cbc91fb64606a077f30fbc4187841`.
Canonicalization used Python `json.dumps(messages, sort_keys=True,
separators=(',', ':'))`, UTF-8 encoded. This checks replay, not completeness.

`amp threads usage <thread-ID> --details` also succeeded on the current thread.
Its human-readable report includes aggregate tokens, model requests, credits,
and orb usage. It explicitly excludes some externally billed provider usage;
do not interpret displayed Amp credits as total model cost or scrape this
display as a stable machine contract without further investigation.

## Local source correction

Inspection found no thread JSON/JSONL/database candidates in the examined Amp
data/config directories. A cache thread log exists, but is not established as
a complete transcript source. This is not proof that no other local state exists.

The checkout already includes `.amp/plugins/coding-trajectory/index.ts` and
`docs/amp-collector.md`. `amp plugins list` reports that collector active, and
it has produced a JSONL file for this thread under
`~/.coding-trajectory/amp/sessions/`. This is CT-generated source, not an Amp
native session log. Earlier inspection of only Amp's directories missed it.

Comparing this active thread's existing CT journal with export exposed an
important mismatch: 47 unique journal message IDs, zero matches to numeric
export `messageId`, and 25 matches to export `protocolMessageID`. In a subsequent
tool-ID comparison, 23 of 33 journal tool IDs were absent from the export
(which had 30 tool IDs). Missing names included `search_messages`,
`read_messages`, and `shell_command`, suggesting internal subagent coverage
differences, but the cause is not established. These were not synchronized
snapshots of the active thread. Do not claim one-to-one source equivalence or
count both representations as independent activity. Investigate provenance
before mapping extra plugin messages into the canonical graph.

## Documented plugin coverage

The stable plugin API provides session/turn/tool events, thread-scoped IDs,
turn outcomes, and simplified transcript messages. `messages({full: true})`
includes compacted-away history; the maximum page size is 20. The current
thread's parent can be queried with `parentThreadID()`.

The message schema does not expose inference token usage or historical message
timestamps. Record hook observation times as observation times only. There is
no `session.end`, and lifecycle hooks have no documented durable replay guarantee.
Graceful disposal is bounded and does not run on crash/SIGKILL.

## Live mechanism: independently checked

The [medium-mode test thread](https://ampcode.com/threads/T-01a07013-7763-72bb-b0ec-176c58048b83)
built and loaded `.amp/plugins/ct-capture-probe.ts`, restricted to its own
thread ID. The probe stores structural information only, not message bodies,
tool arguments/results, thinking, or credentials. The existing CT collector
was not modified. This is a diagnostic probe, not a production uploader: it
reads the full transcript per event and deliberately prioritizes observability
over overhead.

The coordinator downloaded and parsed one sanitized journal snapshot, observing:

- `session.start`: 1; `agent.start`: 1; `agent.end`: 1;
  `tool.call`: 30; `tool.result`: 31.
- The end event reported `done`; the subsequent start event had a different
  initiating message ID, demonstrating a real turn boundary.
- The largest replay contained 58 messages; every snapshot had unique IDs.
- Parent identity was null in every record, despite known parent metadata in
  Amp. The documented API's existence does not establish working edge capture.
- `bun build /tmp/ct-capture-probe-review.ts --target bun` succeeded on the
  downloaded probe source (with output directed to a temporary file).

Counts are one snapshot, not terminal totals. Capture started mid-turn and
inspection happened during a later turn, so unequal call/result counts are
not a complete-run loss measurement. No pause/resume, crash recovery, offline
retry, CT normalization, or remote publication was exercised.

## Support decision

Historical messages are sufficient to justify an Amp adapter prototype rather
than rejecting historical support outright. Full metric parity is not established:
timing, missing usage, internal subagents, parent edges, export schema stability,
and cross-principal permissions remain explicit gaps.

Prefer transcript/export reconciliation for history and plugins for discovery,
live observations, and capture triggers. Do not count the same message twice
when both sources observe it. Keep unknown metrics unknown and retain the
existing body-free shareable upload boundary. Live plugin capture is validated
as a mechanism, not as a complete or lossless provider integration. Before
production support, resolve source reconciliation and parent provenance, then
exercise the canonical adapter and existing upload pipeline end to end.
