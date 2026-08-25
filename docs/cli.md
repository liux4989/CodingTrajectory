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
  turns, and items. Graph commands expose the connected hierarchy; session
  commands select one canonical thread within it.
- **Overview** — compact hierarchy and activity keys for finding where to drill in.
- **Usage** — token accounting, plus cost only when recorded by the source
  session log. Usage is resource accounting, not hierarchy disclosure or
  diagnosis.
- **Schema** — `ct api schema METHOD` prints versioned Pydantic request,
  API-envelope, canonical-result, and optional CLI-presentation JSON Schemas
  without discovering or ingesting sessions.

## Output Formats

The dedicated CLI commands expose readable markdown reports for navigation and
compact JSON for exact data:

- Report commands default to `--output markdown` and accept
  `--output markdown|json`.
- `session request-usage`, `session events`, and `session items` are JSON-only.
  They default to JSON, accept `--output json` for explicit automation, and
  reject `--output markdown`.
- `ct api call`, `ct api batch`, and `ct api schema` are always JSON and do not
  accept an output-format flag.
- `ct api schema METHOD` exits before discovery, ingestion, or cache mutation.
- Dedicated-command JSON is minified. Commands with a compact CLI projection
  use its token-efficient shape; separately modeled CLI schemas are exposed as
  `cli_response`. Raw/detail commands preserve their evidence fields. Compact
  property names remain meaningful: short names are used only
  when established and unambiguous in context, such as `id`, `cwd`, `url`,
  `cmd`, and `pct`. Redundant suffixes may be omitted when the containing object
  supplies the meaning, such as `usage.prompt` instead of
  `usage.prompt_tokens`.

Output and scope flags are leaf-command flags. Put them after the command path,
for example `ct session stats SESSION_ID --output json`; the root form
`ct --output json session stats SESSION_ID` is not supported. `--output` and
`-o` are the supported output flag names.

Dedicated project, session, and graph commands expose typed flags only.
Automation that needs the complete service request surface should use
`ct api call METHOD --params JSON` or `ct api batch`.

Automation that needs service metadata should use `ct api schema METHOD`, for
example `ct api schema session.usage`.

## Service API v2

The service registry exposes 13 methods. Requests are strict: unknown fields
are rejected, and session/graph analysis requires a session, root-session, or
turn entry point. `ct api call` returns the documented
`{id, method, ok, result|error}` envelope; the `result` schema is also exposed
separately for consumers that unwrap it.

Version 2 removes two redundant or unreachable methods:

- `session.turn_usage` is replaced by `session.usage` with `turn_id`.
- `project.logfile` is removed; it never accepted a usable file path through
  `ServiceRuntime`.

`session.model_usage` and `session.tool_usage` are advanced API-only methods.
Human navigation remains on the dedicated session/graph commands; automation
can inspect either method with `ct api schema` and call it through `ct api`.

`session stats` and `session usage` default to markdown reports and switch to
compact JSON with `--output json`.

Token usage payloads follow the glossary in
[`token-usage-glossary.md`](token-usage-glossary.md): provider totals are kept
as reported, while `processed_tokens` and `prompt_completion_tokens` provide
explicit derived totals. New compact CLI JSON uses `prompt_completion` for the
prompt-plus-completion total.

Session and nested graph analysis commands require a session entry-point ID;
they never guess the most-recent graph. Use `project sessions` to choose one.
Explicit session, graph, and turn entry points are located globally through the
cache/index, so those commands have no scope flag. `--global-scope` is reserved
for collection discovery and event-ID queries that do not supply a session
entry point. `project list` always uses the global project index and therefore
has no scope flag.

Session `status` is a reversible liveness signal, with only `living` and
`not_living` values. A session is `living` only while its current canonical
turn is running; completed, interrupted, and stale incomplete turns are all
`not_living`. A resumed or follow-up turn changes the same session back to
`living`. This status does not evaluate task success, acceptance, or outcome.
Use `latest_turn_status` to retain the current/last turn's `running`,
`completed`, `interrupted`, or `incomplete` evidence alongside that liveness
signal.

## Intended Reading Flow

### Structured View

1. `project list [--agent-vendor VENDOR] [--output markdown|json]` — find project names
2. `project sessions [project_name] [--agent-vendor VENDOR] [--global-scope] [--output markdown|json]` — list orchestration runs for a project, get the branch session id to use as an entry point
   - omit `project_name` to use the current directory
   - known agent vendors are `claude_code`, `codex_cli`, and `pi`
3. `session tree <SESSION_ID> [--output markdown|json]` — inspect ordinary human conversation forks and see the spawned-agent count owned by each branch
4. `session overview <SESSION_ID> [--turns N] [--drop-turns K] [--output markdown|json]` — read one thread and identify relevant turns
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity renders as grouped human labels with truncated assistant response previews
   - use `--output json` when you need full item ids for drill-down
5. `session stats <SESSION_ID> [--output markdown|json]` — inspect session stats with compact context/token sections
6. `session usage <SESSION_ID> [--turn TURN_ID] [--output markdown|json]` — inspect turn-level token accounting and request-summed costs
7. `session request-usage <SESSION_ID> [--turn TURN_ID] [--include-context] [--include-causality] [--output json]` — inspect every provider request and its exact usage buckets and estimated cost; opt into the larger context and causal diagnostics
8. `session items <SESSION_ID> [<ITEM_ID> ...] [--turn TURN_ID] [--type ITEM_TYPE] [--include-content] [--output json]` — read scoped item evidence; repeat `--type` to filter item kinds and expand large content only when needed

Graph-level inspection is nested under the branch session that owns the
orchestration run. Ordinary human forks are boundaries and are never aggregated
into the same multi-agent graph:

1. `session graph overview <SESSION_ID> [--turns N] [--drop-turns K] [--narrative] [--output markdown|json]` — inspect the branch root and its spawned-agent threads and structural edges; opt into turn requests and assistant-response narrative
2. `session graph stats <SESSION_ID> [--session-composition] [--output markdown|json]` — inspect aggregate context and token statistics; opt into per-session composition
3. `session graph usage <SESSION_ID> [--turn TURN_ID] [--flat-turns] [--output markdown|json]` — inspect aggregate token usage; opt into the graph-wide flat turn list

The `Other command output` row in `session stats` is scoped to the selected
thread. Use `session graph stats` when child-agent totals are needed; ordinary
forked threads remain separate conversation branches.
Unclassified shell commands are grouped by normalized command name; an
orchestrating `exec` tool is grouped as one wrapper with its contained command
labels. Wrapper output is kept together because the source log does not expose
an exact token split across its inner commands.

`session usage` is intentionally turn-focused. It reports provider token
buckets and prices every recorded provider request independently before
summing turn/session cost. This preserves per-request high-context pricing
thresholds. `session request-usage` is the auditable ledger behind those
rollups; add `--include-context` for request context-window diagnostics or
`--include-causality` for tool-result-to-next-request links.

Item-level tool token attribution is available through the
`session.tool_usage` service method. It is an allocated diagnostic surface and
does not replace the observed totals from `session usage`. Its real-token-cost
rows are allocated only across items in the same turn, so later turns cannot
rewrite an earlier turn's attribution.
The default keeps aggregate and per-tool attribution. Add
`"include":["item_costs"]` for the all-item cost ledger or
`"include":["causality"]` for invoking-response and read-after-result evidence.
There is no dedicated core CLI command for this service method.


### Raw View

1. `session events --event-id EVENT_ID [--event-id EVENT_ID ...] [--global-scope] [--output json]` — resolve the full JSON content of one or more events, including detached tool results; no session ID is required
2. `session events <SESSION_ID> [--turn TURN_ID] --type TYPE [--filter KEY=VALUE] [--output json]` — query raw JSON events by type, optionally narrowed by turn and payload predicates
   - `--type usage` selects provider request-usage observations
   - repeat `--filter` to combine predicates
   - `key=value` requires an exact payload-field match
   - `key=*` requires a payload field to exist
   - `key=!` requires a payload field to be absent or null
   - dot paths such as `result.error=*` are supported

See [`cli-agent-notebook.ipynb`](cli-agent-notebook.ipynb) for an interactive
Jupyter tutorial with examples and workflow guidance.
---
