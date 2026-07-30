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
  turns, and items. This is the progressive-disclosure surface. The session id
  passed to `session ...` commands is the entry point into this connected
  hierarchy.
- **Overview** — compact hierarchy and activity keys for finding where to drill in.
- **Usage** — token accounting, plus cost only when recorded by the source
  session log. Usage is resource accounting, not hierarchy disclosure or
  diagnosis.
- **Schema** — `ct api schema METHOD` prints a versioned Pydantic request and
  response JSON Schema without discovering or ingesting sessions.

## Output Formats

The CLI exposes readable markdown reports for navigation and compact JSON for
exact data:

- Report commands default to `--output markdown`.
- Detail and raw-query commands default to `--output json`.
- `ct api schema METHOD` exits before discovery, ingestion, or cache mutation.
- Any command can switch format with `--output markdown|json`.
- JSON mode is minified and uses a token-efficient public schema. Property
  names remain meaningful; short names are used only when they are established
  and unambiguous in context, such as `id`, `cwd`, `url`, `cmd`, and `pct`.
  Redundant suffixes may be omitted when the containing object supplies the
  meaning, such as `usage.prompt` instead of `usage.prompt_tokens`.

Automation can pass command params as a JSON object with `--params JSON` on
commands that dispatch to the core query surface. Explicit CLI flags override
matching keys from `--params`.

Automation that needs service metadata should use `ct api schema METHOD`, for
example `ct api schema session.usage`.

`session stats` and `session usage` default to markdown reports and switch to
compact JSON with `--output json`.

Token usage payloads follow the glossary in
[`token-usage-glossary.md`](token-usage-glossary.md): provider totals are kept
as reported, while `processed_tokens` and `prompt_completion_tokens` provide
explicit derived totals. New compact CLI JSON uses `prompt_completion` for the
prompt-plus-completion total.

Most session-scoped commands locate sessions automatically from the most-recent
session in the current working directory. Use `--global-scope` on commands that
support it to search all known log files instead. `project list` always uses the
global project index.

## Intended Reading Flow

Structured View
1. `project list [--agent-vendor VENDOR] [--output markdown|json]` — find project names
2. `project sessions [project_name] [--agent-vendor VENDOR] [--output markdown|json]` — list orchestration graphs for a project, get the graph/session id to use as an entry point
   - omit `project_name` to use the current directory
   - known agent vendors are `claude_code`, `codex_cli`, and `pi`
3. `session overview [session_id] [--turns N] [--drop-turns K] [--output markdown|json]` — read one thread and identify relevant turns
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity renders as grouped human labels with truncated assistant response previews
   - use `--output json` when you need full item ids for drill-down
4. `session stats [session_id] [--output markdown|json]` — inspect session stats with compact context/token sections
5. `session usage [session_id] [--turn TURN_ID] [--output markdown|json]` — inspect turn-level token accounting and request-summed costs
6. `session request-usage [session_id] [--turn TURN_ID]` — inspect every provider request, its exact usage buckets, estimated cost, and causal tool links
7. `session items <session_id> [<item_id> ...] [--turn TURN_ID] [--include-content]` — read scoped item evidence; expand large content without re-reading source logs

Graph-level inspection is the orchestration surface. It is the parent of the
individual `session` resource; there is no separate top-level workflow tree:

1. `graph overview [session_id] [--turns N] [--drop-turns K] [--output markdown|json]` — inspect every connected thread and structural edge
2. `graph stats [session_id] [--output markdown|json]` — inspect aggregate context and token statistics for the graph
3. `graph usage [session_id] [--turn TURN_ID] [--output markdown|json]` — inspect aggregate turn-level token usage for the graph

The `Other command output` row in `session stats` is scoped to the selected
thread. Use `graph stats` when child-agent or forked-thread totals are needed.
Unclassified shell commands are grouped by normalized command name; an
orchestrating `exec` tool is grouped as one wrapper with its contained command
labels. Wrapper output is kept together because the source log does not expose
an exact token split across its inner commands.

`session usage` is intentionally turn-focused. It reports provider token
buckets and prices every recorded provider request independently before
summing turn/session cost. This preserves per-request high-context pricing
thresholds. `session request-usage` is the auditable ledger behind those
rollups; use it when you need the request sequence or tool-result-to-next-request
causality.

Item-level tool token attribution is available through the
`session.tool_usage` service method. It is an allocated diagnostic surface and
does not replace the observed totals from `session usage`. Its real-token-cost
rows are allocated only across items in the same turn, so later turns cannot
rewrite an earlier turn's attribution.
There is no dedicated core CLI command for this service method.


Raw View
1. `session events <session_id> --params '{"event_ids": [...]}'` — resolve the full JSON content of one or more events, including detached tool results
2. `session events [session_id] [--turn TURN_ID] --type TYPE [--filter KEY=VALUE]` — query raw JSON events by type, optionally narrowed by turn and payload predicates
   - `--type usage` selects provider request-usage observations
   - repeat `--filter` to combine predicates
   - `key=value` requires an exact payload-field match
   - `key=*` requires a payload field to exist
   - `key=!` requires a payload field to be absent or null
   - dot paths such as `result.error=*` are supported

See [`cli-agent-notebook.ipynb`](cli-agent-notebook.ipynb) for an interactive
Jupyter tutorial with examples and workflow guidance.
---
