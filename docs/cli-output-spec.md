# CLI Output Spec

This document defines human-readable output views for the canonical session API.
The transport and stored resource schemas remain unchanged. CLI views are a
presentation layer on top of the canonical objects.

The CLI auto-discovers coding-agent logs from the supported vendor homes and
ingests them on demand.

## Goals

- Make common reads easy to scan in a terminal.
- Keep the canonical API stable and lossless.
- Support both human and machine consumers.

## Output Modes

### `raw`

Returns the exact canonical API object with no field renaming or projection.

Use for:

- scripts
- debugging
- schema validation

### `summary`

Returns a compact JSON object with the most useful fields for quick inspection.

Rules:

- prefer counts over full nested arrays
- collapse large payloads into previews
- use short display aliases where they improve readability
- omit `null` fields unless they explain state

### `pretty`

Returns terminal-optimized text. This may use labels, indentation, tables, and
sections. It is intended for humans, not for stable parsing.

Rules:

- highlight identity, status, timing, and counts first
- print nested payloads only when small
- truncate long values by default

## Field Aliases

Aliases apply only to `summary` and `pretty`.

| Canonical field | Display alias |
| --- | --- |
| `trajectory_id` | `id` |
| `session_id` | `id` |
| `turn_id` | `id` |
| `event_id` | `id` |
| `parent_session_id` | `parent` |
| `vendor_source` | `vendor` |
| `project_identifier` | `project` |
| `task_reference` | `task` |

## Resource Summaries

### `trajectory.get`

Canonical purpose:
Return one trajectory and the ordered session ids associated with it.

`summary` shape:

```json
{
  "id": "uuid",
  "project": "string | null",
  "task": "string | null",
  "session_count": 2,
  "session_ids": ["uuid", "uuid"]
}
```

Notes:

- include `session_ids` because the object is already small and they are the
  main navigational output
- add `session_count` so callers can scan without counting manually

`pretty` example:

```text
Trajectory  a8a7f6e5-b3bb-46e5-b2eb-7f7a8ad9545a
Project     coding-trajectory
Task        task-42
Sessions    2

1. 3bb69f88-4164-499d-b366-d6a15cb6f88f
2. 7c4bbd8e-c68f-4234-9c9c-1fa9704d5455
```

### `session.get`

Canonical purpose:
Return one session and its mixed replay timeline of turn refs and event refs.

`summary` shape:

```json
{
  "id": "uuid",
  "trajectory": "uuid",
  "parent": "uuid | null",
  "vendor": "codex_cli",
  "started_at": "date-time",
  "ended_at": "date-time | null",
  "status": "completed | in_progress",
  "timeline_count": 3,
  "event_count": 2,
  "turn_count": 1,
  "timeline": [
    {
      "idx": 1,
      "kind": "event",
      "id": "11111111-1111-1111-1111-111111111111",
      "timestamp": "2026-03-13T10:00:05Z",
      "type": "session.started"
    },
    {
      "idx": 2,
      "kind": "turn",
      "id": "22222222-2222-2222-2222-222222222222",
      "started_at": "2026-03-13T10:00:10Z",
      "preview": "Fix the failing test",
      "event_count": 3,
      "events": [
        {
          "idx": 1,
          "id": "33333333-3333-3333-3333-333333333333",
          "timestamp": "2026-03-13T10:00:10Z",
          "type": "user.prompt.submitted",
          "actor": "user"
        },
        {
          "idx": 2,
          "id": "44444444-4444-4444-4444-444444444444",
          "timestamp": "2026-03-13T10:00:15Z",
          "type": "tool.call.requested",
          "actor": "assistant",
          "payload_preview": {
            "tool_name": "exec_command"
          }
        },
        {
          "idx": 3,
          "id": "55555555-5555-5555-5555-555555555555",
          "timestamp": "2026-03-13T10:00:16Z",
          "type": "tool.call.succeeded",
          "actor": "tool"
        }
      ]
    }
  ]
}
```

Derived fields:

- `status = completed` when `ended_at` is present
- `status = in_progress` when `ended_at` is absent
- `event_count` counts all session events, including those wrapped by turns
- `turn_count` counts session turns
- keep `timeline` in canonical order so the summary still explains section flow
- each top-level timeline item is either a standalone session event or a turn
  section

Timeline item rules:

- always include `idx`, `kind`, and `id`
- for top-level event items, include `timestamp`, `type`, and optional `actor`
- include `payload_preview` only when it adds key context, using the same
  preview rules as `event.get`
- for turn items, include `started_at`, a truncated `preview`, `event_count`,
  and a compact nested `events` array
- nested turn events use turn-local `idx` values and omit raw payloads and full
  event bodies
- omit `turn.event_ids` from the summary view because the nested event previews
  already provide the readable sequence

`pretty` example:

```text
Session     3bb69f88-4164-499d-b366-d6a15cb6f88f
Trajectory  a8a7f6e5-b3bb-46e5-b2eb-7f7a8ad9545a
Vendor      codex_cli
Status      completed
Started     2026-03-13T10:00:00Z
Ended       2026-03-13T10:05:00Z
Timeline    3 items (2 events, 1 turn)
```

`pretty` intentionally stays text-first and high level. It may show a separate
timeline table on demand, but it should not replace the structured `summary`
timeline.

Optional pretty timeline table:

```text
IDX  KIND   ID
1    event  11111111-1111-1111-1111-111111111111
2    turn   22222222-2222-2222-2222-222222222222
3    event  33333333-3333-3333-3333-333333333333
```

### `turn.get`

Canonical purpose:
Return one turn and its ordered event ids.

`summary` shape:

```json
{
  "id": "uuid",
  "session": "uuid",
  "user_request": "string | null",
  "started_at": "date-time",
  "ended_at": "date-time | null",
  "status": "completed | in_progress",
  "event_count": 2,
  "preview": "Fix the failing test"
}
```

Derived fields:

- `preview` is the first line of `user_request`, truncated to a CLI-friendly
  width
- `status` follows the same rule as session

`pretty` example:

```text
Turn        22222222-2222-2222-2222-222222222222
Session     3bb69f88-4164-499d-b366-d6a15cb6f88f
Status      completed
Started     2026-03-13T10:00:10Z
Ended       2026-03-13T10:01:00Z
Events      2
Request     Fix the failing test
```

Optional turn events command:

```text
IDX  EVENT ID
1    44444444-4444-4444-4444-444444444444
2    55555555-5555-5555-5555-555555555555
```

### `event.get`

Canonical purpose:
Return one event, either standalone or turn-owned.

`summary` shape:

```json
{
  "id": "uuid",
  "session": "uuid",
  "turn": "uuid | null",
  "timestamp": "date-time",
  "type": "tool.call.requested",
  "actor": "assistant | null",
  "vendor": "codex_cli",
  "provenance": "observed | derived | synthetic",
  "confidence": "high | medium | low",
  "payload_preview": {
    "tool_name": "exec_command",
    "tool_call_id": "call-1"
  }
}
```

`payload_preview` rules:

- include only the first useful identifying fields
- for tool events, prefer `tool_name`, `tool_call_id`, `status`
- for model events, prefer `model`, `request_id`, token counts
- for permission events, prefer `tool_name`, `decision`, `scope`
- cap preview depth to avoid large nested output

`pretty` example:

```text
Event       44444444-4444-4444-4444-444444444444
Type        tool.call.requested
Time        2026-03-13T10:00:15Z
Actor       assistant
Session     3bb69f88-4164-499d-b366-d6a15cb6f88f
Turn        22222222-2222-2222-2222-222222222222
Vendor      codex_cli
Source      observed
Confidence  high

Payload
  tool_name: exec_command
  tool_call_id: call-1
```

## CLI Defaults

Recommended defaults:

- `get` commands default to `summary`
- `session get --view summary` should preserve a compact ordered timeline, not
  collapse to counts only
- `list` or timeline-style commands default to `pretty`
- `--view raw` always returns canonical JSON
- `--json` is an alias for `--view raw`

Recommended flags:

- `--view raw|summary|pretty`
- `--fields field1,field2,...`
- `--no-truncate`

## Query Readability

To keep queries readable:

- accept positional ids when the command scope already implies the resource
- prefer verbs that match the resource model directly
- scope `trajectory list` to the current project by default
- prefer auto-discovery over making users locate log files manually

Examples:

```bash
coding-trajectory trajectory get a8a7f6e5-b3bb-46e5-b2eb-7f7a8ad9545a
coding-trajectory trajectory list
coding-trajectory trajectory list -g
coding-trajectory session get 3bb69f88-4164-499d-b366-d6a15cb6f88f
coding-trajectory session list --trajectory-id a8a7f6e5-b3bb-46e5-b2eb-7f7a8ad9545a
coding-trajectory turn get 22222222-2222-2222-2222-222222222222 --view pretty
coding-trajectory turn list --session-id 3bb69f88-4164-499d-b366-d6a15cb6f88f
coding-trajectory event get 44444444-4444-4444-4444-444444444444 --fields id,type,timestamp,actor
coding-trajectory event list --turn-id 22222222-2222-2222-2222-222222222222
```

Avoid exposing raw JSON-RPC in the default CLI UX.

## Discovery Resolution

Resolution rule:

1. Auto-discover relevant raw logs statelessly for each command.

Auto-discovery homes:

- `~/.codex/sessions`
- `~/.claude/projects`
- `~/.gemini/tmp`
- `~/.local/share/amp/threads`

Discovery behavior:

- every command defaults to current-project matching first
- `-g` widens discovery to all projects
- `list` commands print discovered source paths to stderr
- `get` commands do not print discovery banners by default

## List Commands

Supported operations:

- `trajectory get <uuid> [-g]`
- `trajectory list [-g]`
- `session get <uuid> [-g]`
- `session list [-g] [--trajectory-id <uuid>]`
- `turn get <uuid> [-g]`
- `turn list [-g] [--session-id <uuid>]`
- `event get <uuid> [-g]`
- `event list [-g] [--session-id <uuid>] [--turn-id <uuid>]`

List output rules:

- `trajectory list` defaults to current-project scope by matching the current
  directory name against `project_identifier`
- `-g` disables current-project scoping and lists all projects
- `pretty` uses a table view
- `summary` returns an array of compact JSON objects
- `raw` returns an array of canonical resource objects

## Implementation Notes

- implement summary/pretty transforms in the CLI layer, not in the canonical
  storage model
- keep transforms deterministic and test them directly
- treat `raw` output as the compatibility contract for machine consumers
