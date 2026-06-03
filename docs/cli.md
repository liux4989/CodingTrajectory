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
- YAML for agent-readable hierarchy
- JSON for exact scripting and batch query

## Glossary

- **Session hierarchy** — the structural shape of a run: sessions, child sessions,
  turns, and steps. This is the progressive-disclosure surface.
- **Session tree** — the same session hierarchy when resource usage is projected
  onto it.
- **Overview** — compact hierarchy and activity keys for finding where to drill in.
- **Narrative** — deterministic user/assistant/tool activity for summarizers.
- **Usage** — token, cost, quota, duration, and output-size accounting. Usage is
  resource accounting, not hierarchy disclosure.

## Output Formats

The CLI exposes two structured stdout formats:

- `--format yaml` — best for agent and human reading. Use this for hierarchy,
  narrative, and drill-down orientation.
- `--format json` — best for exact machine use. Use this for `jq`, batch
  scripts, schema checks, and saved artifacts.

`--output FILE` always writes JSON, regardless of stdout format. This keeps file
output stable for automation while allowing YAML to optimize interactive agent
reading.

`session stats` and `session usage` also retain their compact `text` stdout
mode for terminal summaries.

## Intended Reading Flow

Structured View
1. `project list` — find project names
2. `project sessions <project_name>` — list sessions for a project, get the session id to use as an entry point
3. `session overview <session_id> [--turns N] [--drop-turns K] [--format yaml|json]` — read the compact session hierarchy, identify relevant steps
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity uses compact render keys such as `text`, `tool`, `path`, `query`, `url`, `count`, and plural variants
   - repeated consecutive low-value tool calls are grouped by tool profile with ordered unique targets and repeat counts when useful
   - mutating or high-signal tools such as edits, writes, shell commands, subagents, and handoffs stay ungrouped; use `session overview --view narrative` or `session step-detail` to expand the evidence
4. `session overview --view narrative <session_id> [--turns N] [--drop-turns K] [--format yaml|json]` — read deterministic user/assistant/tool activity for summarization
5. `session stats <session_id> [--format text|yaml|json]` — inspect compact context/token usage composition
6. `session usage <session_id> [--turn TURN_ID] [--format text|yaml|json]` — compare turn-level activity cost and token efficiency
7. `session step-detail <step_id> [--format yaml|json]` — read the evidence for a specific step

`session usage` is intentionally turn-focused. It reports coarse activity,
token buckets, cache reuse, output/input efficiency, and cost drivers without
expanding paths, queries, commands, or individual tool calls. Use
`session overview`, `session step-detail`, or `session event-detail` when you
need tree/navigation detail.


Raw View
1. `session event-detail <event_id> [--format yaml|json]` — resolve the full content of an event
2. `session event-scan <session_id> --type TYPE [--filter ...] [--format yaml|json]` — query raw events by type, optionally narrowed by payload predicates

See [`cli-agent-notebook.md`](cli-agent-notebook.md) for an agent-oriented
tutorial that uses YAML for reading and JSON for exact queries.
---
