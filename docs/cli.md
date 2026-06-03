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

## Glossary

- **Session hierarchy** — the structural shape of a run: sessions, child sessions,
  turns, and steps. This is the progressive-disclosure surface.
- **Session tree** — the same session hierarchy when resource usage is projected
  onto it.
- **Overview** — compact hierarchy and activity keys for finding where to drill in.
- **Narrative** — deterministic user/assistant/tool activity for summarizers.
- **Usage** — token, cost, quota, duration, and output-size accounting. Usage is
  resource accounting, not hierarchy disclosure.


## Intended Reading Flow

Structured View
1. `project list` — find project names
2. `project sessions <project_name>` — list sessions for a project, get the session id to use as an entry point
3. `session overview <session_id> [--turns N] [--drop-turns K]` — read the compact session hierarchy, identify relevant steps
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity uses compact render keys such as `text`, `tool`, `path`, `query`, `url`, `count`, and plural variants
   - repeated consecutive low-value tool calls are grouped by tool profile with ordered unique targets and repeat counts when useful
   - mutating or high-signal tools such as edits, writes, shell commands, subagents, and handoffs stay ungrouped; use `session overview --view narrative` or `session step-detail` to expand the evidence
4. `session overview --view narrative <session_id> [--turns N] [--drop-turns K]` — read deterministic user/assistant/tool activity for summarization
5. `session stats <session_id>` — inspect token/cost rollups joined to the session tree
6. `session turn-usage <turn_id>` — inspect token/cost usage for one turn, with compact step token deltas
7. `session tool-usage <session_id>` — inspect tool-step cost boundaries and per-tool output-size signals
8. `session step-detail <step_id>` — read the evidence for a specific step

`session tool-usage` keeps billing boundaries explicit:
`observed_step_cost` is the
measured/estimated cost for the enclosing tool step, while each individual tool
entry only reports output-size signals. Individual shell commands do not have
separate observed costs.


Raw View
1. `session event-detail <event_id>` — resolve the full content of an event
2. `session event-scan <session_id> --type TYPE [--filter ...]` — query raw events by type, optionally narrowed by payload predicates
---
