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

## Versioned Service API

The accepted additive redesign for session interpretation and evidence
discovery is specified in
[`session-api-redesign.md`](session-api-redesign.md). It adds
`session.summary.v1` and `session.search.v1` without changing the existing v2
methods.

The service registry currently exposes 25 method-scoped contracts: 16 project,
session, and graph methods; two living protocols; and seven estimation methods.
Requests are strict: unknown fields are rejected. The new summary/search
methods require an exact canonical session ID; historical v2 session/graph
analysis accepts a session, root-session, or turn entry point.
`ct api call` returns the documented
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
Explicit session, graph, and turn entry points are resolved through the
Supabase resource index, so those commands have no scope flag. `--global-scope`
is reserved for project collection discovery. Evidence reads require a published
session or turn scope. `project list` always uses the workspace project inventory
and therefore has no scope flag.

Session `status` is a reversible liveness signal, with only `living` and
`not_living` values. A session is `living` only while its current canonical
turn is running; completed, interrupted, and stale incomplete turns are all
`not_living`. A resumed or follow-up turn changes the same session back to
`living`. This status does not evaluate task success, acceptance, or outcome.
Use `latest_turn_status` to retain the current/last turn's `running`,
`completed`, `interrupted`, or `incomplete` evidence alongside that liveness
signal.

## Local and remote execution

Every service API reads the same canonical Supabase workspace, including local
CLI calls, embedded `ServiceRuntime`/plugin clients, and the HTTP API. Local
logs are ingestion inputs and optional evidence; they are never an alternative
API inventory or metrics authority. Unpublished sessions do not appear in reads.
Missing credentials and unavailable database state fail explicitly without local
fallback. Eligible local session queries can publish missing or updated data
before returning its Supabase result, as described below.

The CLI uses `CT_SUPABASE_URL`, `CT_SUPABASE_ANON_KEY`, `CT_ACCESS_TOKEN`, and
`CT_REMOTE_WORKSPACE_ID`. Setting `CT_CREDENTIAL_PROFILE` explicitly selects a
profile and obtains a fresh token for each CLI read, superseding connection and
token environment variables (including an expired `CT_ACCESS_TOKEN`). Profiles
use macOS Keychain or an injected password environment variable on headless hosts.
Without an explicit profile, absent connection credentials select profile
`default`; partial environment credentials are rejected. Embedded clients require the four
Supabase environment variables. `--remote-workspace-id` on `ct api call/batch`
is a workspace override, not a backend switch.

```sh
ct api call project.sessions \
  --params '{"project_name":"CodingTrajectory","since_days":7}'
```

Add `--snapshot-sequence N` to pin a published workspace sequence; otherwise
runtime creation resolves the latest sequence. Historical responses identify
that database source and snapshot, even when the caller runs locally.

Historical reads fetch published `ct.shareable_graph.v1` artifacts through
Supabase PostgREST RPCs. Python validates their identity, digest, and schema,
reconstructs an in-memory graph, and executes the shared handlers. Local callers
use this exact pipeline; they no longer assemble another graph from local logs
for ordinary reads. The database state reflects the last publication.
Artifact caches live within a runtime and are keyed by method and scope;
separate CLI calls create fresh runtimes. Batch calls share one runtime.
The default plugin client also resolves a fresh snapshot per call. An explicitly
owned read-only `ServiceRuntime` stays pinned until the caller creates a new
runtime. On-demand publication can advance an unpinned local runtime.

Content is excluded by default on both surfaces. Explicit `session.search`,
`session.events`, `session.items` with `include_content=true`, and
`graph.overview` with `include:["narrative"]` request local evidence. A local
caller first resolves the published session, then lazily loads host evidence
and checks its retained canonical facts against the selected publication.
Missing or mismatched evidence is an error. Hydration never changes the cached
canonical snapshot or its default responses. The response identifies
`content_scope: local_evidence` and `evidence_source: local` separately from the
canonical database source.

Evidence calls need a published session/root-session/turn scope; event IDs or
item IDs alone cannot authorize local discovery. A changed session must be
published before its new content can be loaded. Matching retained facts does
not attest omitted body bytes: raw logs remain the local evidence authority.
The HTTP runtime has no local evidence loader and rejects all four content
requests, even if the server machine has those logs. Clients cannot enable that
capability through request parameters. `ServiceRuntime(local_evidence=False)`
also disables evidence loading for embedded callers.

All 25 registered service methods are covered below. The registry in
`packages/core/src/coding_trajectory/contracts/registry.py` is authoritative.

| Methods | Remote behavior |
| --- | --- |
| `project.list` | Remote project inventory RPC |
| `project.sessions` | Published historical artifacts, filtered by request scope |
| `session.overview`, `session.summary`, `session.tree`, `graph.overview` | Historical artifact plus Python projection; content omitted by default; graph narrative requires local evidence |
| `session.stats`, `graph.stats`, `session.usage`, `graph.usage`, `session.model_usage`, `session.request_usage`, `session.tool_usage` | Historical artifact plus Python projection; numeric measurements preserved |
| `session.items` | Metadata-only remote support; `include_content=true` is rejected |
| `session.events`, `session.search` | Lazy local evidence for a published scope; HTTP calls are rejected |
| `living.events`, `living.sessions` | Remote living authority RPCs; availability depends on published living state |
| `estimate.predict`, `estimate.bind`, `estimate.backfill.start` | Remote estimation operations that create or update state |
| `estimate.get`, `estimate.list`, `estimate.calibration` | Remote estimation reads that can also persist refreshed actual comparisons |
| `estimate.backfill.status` | Remote job status; requires an existing job ID |

`ct api serve --remote-workspace-id "$CT_REMOTE_WORKSPACE_ID"` exposes
authenticated `POST /v1/call`, `POST /v1/batch`, and `POST /v1/schema` endpoints
(default bind: `127.0.0.1:8765`). Requests need a bearer token. Local
`ct api schema METHOD` remains offline and does not need credentials.

### Fresh-session queries

Local queries with an explicit `session_id` or `root_session_id` automatically
synchronize that session's graph when eligible local sources are available.
The configured Keychain profile supplies the collector identity. Embedded or
environment-configured callers also set `CT_COLLECTOR_AGENT_ID` and need the
corresponding collector capabilities. HTTP calls never get this capability.

```text
query Supabase -> locate eligible local graph sources
  -> unchanged: use the published result
  -> missing/changed: fence and publish the selected graph
       -> verify the committed artifact -> read a fresh Supabase snapshot
```

The source window is seven days and the scope is the current project. Required
parent/fork inputs are used for normalization, but unrelated canonical graphs
are not published. Missing dependencies or an incomplete overlapping graph
produce an explicit error; the query never widens its scope automatically.
Source fingerprints avoid repeated normalization for unchanged files, and a
canonical artifact comparison avoids re-publishing unchanged facts.

Concurrent local queries share an agent publication lock and recheck the database
after waiting. Exact retry state lives under the private
`~/.coding-trajectory/control-plane/on-demand/` directory, separate from earlier
collector databases. `CT_ON_DEMAND_STATE_DIR` can override that directory. A
pending attempt must be retried for its original session before another target
can use that on-demand stream. Normal CLI collector runs use the same agent lock.

Progress goes to stderr. Stdout retains the existing result format. Failures
return an error instead of an unpublished local response. This first read pays
publication latency; it is not an instant local preview. Appends after the fence
are left for a subsequent query or batch publication.

`CT_AUTO_PUBLISH=0` disables on-demand writes. An explicit
`--snapshot-sequence N` also disables them. A local API batch prepares eligible
publications first, then executes its reads at one final snapshot. Collection
queries and turn/item/event-ID-only queries do not initiate publication.

For an explicit targeted collector pass:

```sh
ct collector run --credential-profile default --project-name CodingTrajectory \
  --session-id SESSION_ID --since-days 7 --state-path "$CT_COLLECTOR_STATE"
```

Use the existing verified collector state for manual passes. The automatic
on-demand state recovers remote source and publication watermarks on first use.

### Benchmarking remote reads

```sh
uv run python scripts/benchmark-remote-api.py \
  --profile default --project CodingTrajectory --since-days 7 --repeat 3
```

Omit `--profile` to use the four environment variables above. The script uses an
ordinary authenticated user, pins a snapshot, and disables local log resolution.
It selects the first graph in the bounded project collection, or accepts an
explicit `--session-id` within that scope. Project inventory and living-session
inventory are workspace-wide; living events are scoped to the selected session.
Estimation methods are recorded as skipped because of side effects or required
job identifiers. Local-only rejection checks are separate from successful reads.

The aggregate-only report is written to `.artifacts/benchmarks/remote-api.json`.
For each query it measures one fresh runtime (including snapshot lookup) and
repeated calls on the same runtime. Both include response validation and JSON
serialization; neither includes CLI startup or the HTTP facade. Reused historical
calls can avoid downloading the artifact. These samples measure latency for one
graph, not concurrency, throughput, or a population percentile. The older
`scripts/benchmark-query.py` measures local store/projection costs only.

## Intended Reading Flow

### Structured View

1. `project list [--agent-vendor VENDOR] [--output markdown|json]` — find project names
2. `project sessions [project_name] [--agent-vendor VENDOR] [--global-scope] [--output markdown|json]` — list orchestration runs for a project, get the branch session id to use as an entry point
   - omit `project_name` to use the current directory
   - known agent vendors are `claude_code`, `codex_cli`, and `pi`
3. `session tree <SESSION_ID> [--output markdown|json]` — inspect ordinary human conversation forks and see the spawned-agent count owned by each branch
4. `session summary <SESSION_ID> [--turn TURN_ID] [--output markdown|json]` — get a bounded brief with canonical evidence references
5. `session overview <SESSION_ID> [--turns N] [--drop-turns K] [--output markdown|json]` — read one thread and identify relevant turns
   - `--turns N` keeps only the last N visible turns per session
   - `--drop-turns K` drops the last K visible turns per session, matching `thread/rollback numTurns=K`
   - when combined, `--drop-turns` is applied before `--turns`
   - activity renders as grouped human labels with truncated assistant response previews
   - use `--output json` when you need full item ids for drill-down
6. `session search <SESSION_ID> <QUERY> [--turn TURN_ID] [--mode text|path] [--kind KIND] [--limit N] [--output markdown|json]` — find canonical evidence using deterministic structural and lexical ranking
7. `session items <SESSION_ID> [<ITEM_ID> ...] [--turn TURN_ID] [--type ITEM_TYPE] [--include-content] [--output json]` — expand scoped item evidence; repeat `--type` to filter item kinds and expand large content only when needed
8. `session stats <SESSION_ID> [--output markdown|json]` — inspect session stats with compact context/token sections
9. `session usage <SESSION_ID> [--turn TURN_ID] [--output markdown|json]` — inspect compact request, input/cache, output/reasoning, processed-token, and request-summed cost totals by model and turn
10. `session request-usage <SESSION_ID> [--turn TURN_ID] [--include-context] [--include-causality] [--output json]` — inspect every provider request and its exact usage buckets and estimated cost; opt into the larger context and causal diagnostics

Graph-level inspection is nested under the branch session that owns the
orchestration run. Ordinary human forks are boundaries and are never aggregated
into the same multi-agent graph:

1. `session graph overview <SESSION_ID> [--turns N] [--drop-turns K] [--narrative] [--output markdown|json]` — inspect the branch root and its spawned-agent threads and structural edges; opt into turn requests and assistant-response narrative
2. `session graph stats <SESSION_ID> [--session-composition] [--output markdown|json]` — inspect aggregate context and token statistics; opt into per-session composition
3. `session graph usage <SESSION_ID> [--turn TURN_ID] [--flat-turns] [--output markdown|json]` — inspect aggregate token usage; opt into the graph-wide flat turn list

The `Command output` and `Other tool output` rows in `session stats` are scoped
to the selected thread. Use `session graph stats` when child-agent totals are
needed; ordinary forked threads remain separate conversation branches. Command
output remains one observed structural bucket instead of being divided by an
inferred command-intent taxonomy. An orchestrating `exec` tool remains one
wrapper because the source log does not expose an exact token split across its
inner commands.

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

1. `session events SESSION_ID --event-id EVENT_ID [--event-id EVENT_ID ...] [--output json]` — lazily resolve full JSON events within a published session on the originating host
2. `session events <SESSION_ID> [--turn TURN_ID] --type TYPE [--filter KEY=VALUE] [--output json]` — query raw JSON events by type, optionally narrowed by turn and payload predicates
   - `--type usage` selects provider request-usage observations
   - repeat `--filter` to combine predicates
   - `key=value` requires an exact payload-field match
   - `key=*` requires a payload field to exist
   - `key=!` requires a payload field to be absent or null
   - dot paths such as `result.error=*` are supported

---
