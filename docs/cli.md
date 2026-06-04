# CLI Design

## Goal

The CLI has one job: help an LLM analyze coding-agent logs without reading
the full raw log stream first.

Three goals:

1. Progressive disclosure — read the big picture first, drill into details only when needed.
2. Contextual understanding — return enriched, post-processed structure instead of raw events.
3. Noise removal — suppress execution-recording noise by default.

## Design Principle

- session hierarchy for orientation
- evidence of atomic action for detail
- text reports for reading
- JSON for exact scripting and batch query

## Glossary

- **Session hierarchy** — the structural shape of a run: sessions, child sessions,
  turns, and steps. This is the progressive-disclosure surface. The session id
  passed to `session ...` commands is the entry point into this connected
  hierarchy.
- **Overview** — compact hierarchy and activity keys for finding where to drill in.
- **Usage** — token and cost accounting. Usage is resource accounting, not
  hierarchy disclosure or diagnosis.

## Output Formats

The CLI exposes readable reports for navigation and JSON for exact data:

- Report commands default to human-readable stdout. Use `--data` to print the
  structured JSON projection behind the report.
- Detail and raw-query commands print JSON.
- `project` commands accept `--format json|overview`; the default is `json`.
- Detail and raw-query commands currently accept `--format json|overview` for
  command-shape consistency, but their stdout payload is JSON.

`--output FILE` always writes JSON, regardless of stdout format. This keeps file
output stable for automation while allowing stdout to optimize interactive
reading.

`session stats` and `session usage` use fixed reports for readable stdout and
`--data` or `--output` for exact JSON.

Most session-scoped commands locate sessions automatically from the most-recent
session in the current working directory. Use `--global-scope` on commands that
support it to search all known log files instead. `project list` always uses the
global project index.

## Intended Reading Flow

Structured View
1. `project list [--agent-vendor VENDOR] [--format json|overview]` — find project names
2. `project sessions [project_name] [--agent-vendor VENDOR] [--format json|overview]` — list sessions for a project, get the session id to use as an entry point
   - omit `project_name` to use the current directory
   - known agent vendors are `claude_code`, `codex_cli`, and `amp`
3. `session overview [session_id] [--turns N] [--drop-turns K] [--data]` — read the compact session hierarchy, identify relevant turns
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity renders as grouped human labels with truncated assistant response previews
   - use `--data` when you need full step ids for drill-down
4. `session stats [session_id] [--extra-billing] [--data]` — inspect session stats with compact context/token sections
5. `session usage [session_id] [--turn TURN_ID] [--extra-billing] [--data]` — inspect turn-level activity token and cost accounting
6. `session step-detail <step_id> [...]` — read the JSON evidence for one or more steps

`session usage` is intentionally turn-focused. It reports token buckets and cost
grouped by turn and activity category without expanding paths, queries,
commands, individual tool calls, derived efficiency, or explanatory semantics.
Use `session overview`, `session step-detail`, `session event-detail`, or
detail commands when you need hierarchy/navigation detail or causal drill-down.


Raw View
1. `session event-detail <event_id>` — resolve the full JSON content of an event
2. `session event-scan [session_id] --type TYPE [--filter KEY=VALUE]` — query raw JSON events by type, optionally narrowed by payload predicates
   - repeat `--filter` to combine predicates
   - `key=value` requires an exact payload-field match
   - `key=*` requires a payload field to exist
   - `key=!` requires a payload field to be absent or null
   - dot paths such as `result.error=*` are supported

See [`cli-agent-notebook.ipynb`](cli-agent-notebook.ipynb) for an interactive
Jupyter tutorial with examples and workflow guidance.
---
