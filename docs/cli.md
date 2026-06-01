# CLI Design

## Goal

The CLI has one job: help an LLM analyze coding-agent logs without reading
the full raw log stream first.

Three goals:

1. Progressive disclosure — read the big picture first, drill into details only when needed.
2. Contextual understanding — return enriched, post-processed structure instead of raw events.
3. Noise removal — suppress execution-recording noise by default.

## Design Principle

- navigation tree for orientation
- evidence of atomic action for detail


## Intended Reading Flow

Structured View
1. `project list` — find project names
2. `project graphs <project_name>` — list session graphs for a project, get the session id to use as an entry point
   - or `project --logfile PATH` — load a log file directly and get the session id
3. `session overview <session_id> [--turns N] [--drop-turns K]` — read the navigation tree, identify relevant steps
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
4. `session narrative <session_id> [--turns N] [--drop-turns K]` — read deterministic user/assistant/tool activity for summarization
5. `graph usage <session_id>` — inspect token/cost rollups joined to the hierarchy
6. `graph turn-usage <session_id>` — compare token/cost usage turn by turn, with compact step token deltas
7. `graph tool-usage <session_id>` — inspect tool-step cost boundaries and per-tool output-size signals
8. `step detail <step_id>` — read the evidence for a specific step

`graph tool-usage` keeps billing boundaries explicit: `observed_step_cost` is the
measured/estimated cost for the enclosing tool step, while each individual tool
entry only reports output-size signals. Individual shell commands do not have
separate observed costs.

Compatibility aliases remain available: `graph metrics`, `graph turns`, and
`graph tools`.


Raw View
1. `event detail <event_id>` — resolve the full content of a step
2. `event scan <session_id> --type TYPE [--filter ...]` — query raw events by type, optionally narrowed by payload predicates
---
