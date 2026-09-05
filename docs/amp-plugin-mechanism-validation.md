# Amp Plugin Mechanism Validation

Validated on 2026-09-05 in the medium-mode child thread
`T-01a07013-7763-72bb-b0ec-176c58048b83`. This is a bounded mechanism probe,
not a second production collector. The existing collector remains
`.amp/plugins/coding-trajectory/index.ts`.

## Probe and privacy boundary

The project-local `.amp/plugins/ct-capture-probe.ts` registers observers for
`session.start`, `agent.start`, `tool.call`, `tool.result`, and `agent.end`. It is
hard-gated to the validation thread. `tool.call` always returns `allow`,
`tool.result` returns no replacement, `agent.start` adds no message, and
`agent.end` never returns `continue`.

Records are written mode `0600` to the ignored local file
`.amp/local/ct-capture-probe/T-01a07013-7763-72bb-b0ec-176c58048b83.jsonl`.
They contain IDs, roles, block types, statuses, field names, counts, pagination,
observed timestamps, and the observed parent value. They do not contain prompt,
thinking, tool input/output, error, or tool-name bodies. A structural `jq` scan
found no body-bearing scalar keys.

## Observations

- Loading the probe reported all five event registrations active and no tools,
  commands, or agent modes.
- The first completed turn emitted `agent.end` at
  `2026-09-05T05:43:01.855Z`, status `done`, with 36 messages read in pages of
  20 and 16. The next prompt arrived after that turn completed and emitted
  `agent.start` at `2026-09-05T05:43:06.071Z`, with 37 messages read in pages of
  20 and 17.
- A successful shell tool ran in each turn. A nonzero shell exit and a missing
  media read exercised failure-like outcomes. `tool.result` event status was
  still `done`, while replayed `tool_result` blocks represented the missing
  media operation as `error`; consumers must not treat those status surfaces as
  equivalent.
- Every capture called
  `thread.messages({full:true, from:'start', offset, limit:20})` until a short
  page. The replay crossed first two and later three pages. Message IDs were
  unique, and the 19 IDs in the first snapshot remained in the same order in a
  later 27-message snapshot.
- At the second-turn `agent.start` boundary, the probe replay's 37 stable
  message IDs exactly matched the 37 unique `message.id` values then persisted
  by the existing collector. This establishes agreement for that bounded
  snapshot only.
- `parentThreadID()` returned `null` in every probe capture. The existing
  collector also persisted `parent_thread_id: null`. The validation therefore
  records an observed null; it does not claim that parent identity is available
  even though orchestration metadata identifies a parent thread.

## Export comparison and limitations

Amp's plugin transcript uses stable string `id` values. A separate parent-side
export comparison reported 47 unique plugin message IDs: 25 matched export
`protocolMessageID`, while none matched numeric export `messageId`. It also
reported 33 plugin tool IDs versus 30 export tool IDs, with 23 plugin IDs absent
from the export, including internal transcript reads and shell calls. The export
artifact was not present in this checkout, so those counts are external
comparison evidence rather than a locally reproduced result.

Do not merge the sources as if they were 1:1. In particular, do not join plugin
`id` to numeric `messageId`; `protocolMessageID` is only a partial observed
match. The mismatch may reflect export omission of subagent/internal content or
probe contamination, and this validation does not distinguish those causes.

The plugin API replay contains no provider usage, token counts, cost, inference
timestamps, or complete model-routing metadata. `observed_at` is the probe's
wall-clock observation time, not a message or inference timestamp. Those fields
require a separate enrichment source and must not be inferred.
