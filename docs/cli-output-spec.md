# CLI Output Spec

This document defines output views for the canonical session API. The CLI reads
from richer stored resource models, then projects them into the API contract
for `raw` output and into a presentation-friendly shape for `pretty`.

The CLI auto-discovers coding-agent logs from the supported vendor homes and
ingests them on demand.

## Goals

- Make common reads easy to scan in a terminal.
- Keep the canonical API stable and lossless.
- Support both human and machine consumers.

## Output Modes

### `raw`

Returns the canonical API detail object for the requested resource, matching
`docs/session-api.json`.

Use for:

- scripts
- debugging
- schema validation

### `pretty`

Returns curated JSON with the most useful fields. Both human-scannable and
agent-parseable.

Rules:

- prefer counts over full nested arrays
- collapse large payloads into previews
- use short display aliases
- omit null fields
- truncate long values by default

## Field Aliases

Aliases apply only to `pretty`.

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

`pretty` shape:

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

### `session.get`

Canonical purpose:
Return one session and its mixed replay timeline of turn refs and event refs.

`pretty` shape:

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
      "event_count": 3
    }
  ]
}
```

Derived fields:

- `status = completed` when `ended_at` is present
- `status = in_progress` when `ended_at` is absent
- `event_count` counts all session events, including those wrapped by turns
- `turn_count` counts session turns
- keep `timeline` in canonical order so the pretty view still explains section flow
- each top-level timeline item is either a standalone session event or a turn
  ref
- use `turn get` to inspect the full contents of a turn

Timeline item rules:

- always include `idx`, `kind`, and `id`
- for top-level event items, include `timestamp`, `type`, and optional `actor`
- include `payload_preview` only when it adds key context, using the same
  preview rules as `event.get`
- for turn items, include `started_at`, a truncated `preview`, and
  `event_count`
- do not inline nested turn events in `session.get`; turn expansion belongs to
  `turn get`

### `turn.get`

Canonical purpose:
Return one turn and its ordered event ids.

`pretty` shape:

```json
{
  "id": "uuid",
  "session": "uuid",
  "user_request": "string | null",
  "started_at": "date-time",
  "ended_at": "date-time | null",
  "status": "completed | in_progress",
  "event_count": 2,
  "event_ids": ["uuid", "uuid"],
  "preview": "Fix the failing test"
}
```

Derived fields:

- `preview` is the first line of `user_request`, truncated to a CLI-friendly
  width
- `status` follows the same rule as session
- `turn get --view pretty` includes `event_ids`

### `event.get`

Canonical purpose:
Return one event, either standalone or turn-owned.

`pretty` shape:

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
- for `session.get` timeline summaries, previews may backfill `tool_name` or
  `model` from other events in the same session when the event payload only
  includes `tool_call_id` or `request_id`
- cap preview depth to avoid large nested output

## CLI Defaults

Recommended defaults:

- both `get` and `list` default to `pretty`
- `session get --view pretty` should preserve a compact ordered timeline, not
  collapse to counts only
- `--view raw` always returns canonical JSON
- `--json` is an alias for `--view raw`

Recommended flags:

- `--view raw|pretty`
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
coding-trajectory event get 44444444-4444-4444-4444-444444444444 --fields id,type,timestamp,actor
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
- `event get <uuid> [-g]`

List output rules:

- `trajectory list` defaults to current-project scope by matching the current
  directory name against `project_identifier`
- `-g` disables current-project scoping and lists all projects
- `pretty` returns an array of compact JSON objects
- `raw` returns an array of canonical resource objects

## Implementation Notes

- implement pretty transforms in the CLI layer, not in the canonical
  storage model
- keep transforms deterministic and test them directly
- treat `raw` output as the compatibility contract for machine consumers
