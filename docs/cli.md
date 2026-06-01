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
3. `graph overview <session_id> [--turns N] [--drop-turns K]` — read the navigation tree, identify relevant steps
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
4. `graph narrative <session_id> [--turns N] [--drop-turns K]` — read deterministic user/assistant/tool activity for summarization
5. `metrics graph <session_id>` — inspect native token/quota metrics joined to the hierarchy
6. `metrics turns <session_id>` — compare execution metrics turn by turn
7. `step detail <step_id>` — read the evidence for a specific step


Raw View
1. `event detail <event_id>` — resolve the full content of a step
2. `event scan <session_id> --type TYPE [--filter ...]` — query raw events by type, optionally narrowed by payload predicates
---
