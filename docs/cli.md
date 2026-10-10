# CLI guide

Use `ct` to find a session, read its context, and inspect retained evidence.
Run examples from the repository root after `uv sync --all-packages --locked`.
Replace uppercase IDs and shell variables with values for your selected source.

## Select a source

Put `--source` before the command path:

```sh
uv run ct --source local project list
uv run ct --source auto project list
```

- `local` reads host-local sources without remote fallback.
- `auto` (the default) reads local sources only.
- `shared` and `remote` are recognized but fail explicitly with
  `method_unavailable`; they never silently read local evidence.

There is no remote fallback, connection profile, or credential configuration.
Queries never publish data. The `collector`, `connection`, and `api serve`
commands are no longer supported.

Historical and inventory queries discover fresh topology per request. Only
selected runs and their canonical dependencies are ingested, retained, and
redacted in memory, with reuse limited to one request or explicit API batch.
There is no cross-request topology or graph cache; Core no longer uses
`~/.coding-trajectory/local.sqlite`. Repeated detail reads of large runs repeat
ingestion. Native metrics and public vNext cursors stay unchanged.

Commands accepting `--agent-vendor` support the Core `Vendor` values:
`codex_cli`, `claude_code`, `pi`, and `amp`. For example:

```sh
uv run ct --source local project sessions --agent-vendor amp --limit 20
```

## Read a session

1. List projects:

   ```sh
   uv run ct --source local project list --output json
   ```

2. List sessions for an ID returned by that source:

   ```sh
   uv run ct --source local project sessions --project-id PROJECT_ID --limit 20
   ```

3. Read the selected session brief:

   ```sh
   uv run ct --source local session summary SESSION_ID
   ```

4. Read its recent chronology:

   ```sh
   uv run ct --source local session overview SESSION_ID --turns 5
   ```

5. Resolve a relevant item:

   ```sh
   uv run ct --source local session items SESSION_ID ITEM_ID --output json
   ```

6. Resolve an event when its envelope is needed:

   ```sh
   uv run ct --source local session events SESSION_ID --event-id EVENT_ID --output json
   ```

Project names are convenience selectors, not identity. Use an ID when names are
ambiguous. Local project IDs identify host locations; moving a directory changes
its ID.

Inventory, overview, search, and detail responses support pagination. Pass
`next_cursor` as `--cursor` and keep the method, scope, selectors, and filters
unchanged; page size may change. Cursors are query-bound continuations over live local evidence, not
snapshot pins. Evidence can change between pages; no immutable view selection
is supported.

`project sessions` returns discovery cards: session identity, project, title and
preview, vendors, member session IDs, and modification time. Read session usage
or stats separately for measurements; cards do not include usage, runtime, or
warnings.

## Choose the correct view

| Command after `ct` | Purpose |
| --- | --- |
| `session tree SESSION_ID` | Conversation forks and branch structure |
| `session summary SESSION_ID` | Objective, outcome, verification, and unresolved work |
| `session overview SESSION_ID` | Chronological turns and activity references |
| `session search SESSION_ID QUERY --mode text` | Search retained evidence locally |
| `session search SESSION_ID PATH --mode path` | Find retained path references locally |
| `session items SESSION_ID --turn TURN_ID` | Retained item details within one turn |
| `session events SESSION_ID --type usage` | Normalized event envelopes |
| `session stats SESSION_ID` | Time, status, counts (including compactions), and visible context |
| `session usage SESSION_ID --turn TURN_ID` | Recorded token usage and reported or estimated cost |
| `session request-usage SESSION_ID` | Recorded usage and cost for each provider request |
| `session graph overview SESSION_ID` | Orchestration topology |
| `session graph stats SESSION_ID` | Graph totals and per-session statistics |
| `session graph usage SESSION_ID` | Recorded token usage and costs across graph sessions |

Session methods normally require an exact session ID. `turn_id` narrows that
session; it is not a replacement session selector. Tree and graph commands can
resolve an entry point in the same lineage or orchestration run.
Ordinary conversation forks do not enter the parent's graph aggregate.

Token reports use the [glossary's common terms](token-usage-glossary.md):
`fresh input`, `cached input`, `cache write`, `output`, `reasoning`, and
`processed total`. When fresh input is unknown, the label is `prompt`, which may
include cache. Audit lines can also show `reported total` and
`prompt + output (including cache)`. Costs are labeled `reported cost` or
`estimated cost`.

Context categories use visible-token estimates.
Category percentages use the full context-window capacity. An unknown window
size leaves the percentage unavailable. CT reconstructs categories from logged
evidence; it does not use Claude's `/context` output as a metric source.
Provider input counts describe the latest request; recorded usage adds up entries
across the session. Repeated Claude response IDs each contribute to recorded usage,
so these totals may differ from billed usage. JSON field names stay stable;
the glossary maps them to the display terms.

`session stats` puts the model, latest-request context, recorded token usage,
execution time with estimated LLM and tool time, activity, and compaction count
first. The full category tree uses a narrow table with estimated visible tokens
and their share of the context.
Claude starting context uses instruction, memory, skill, MCP, and prompt-snapshot
attachments recorded before the first API response. If the initial snapshot
omits tool schemas, CT recovers non-deferred definitions from the next matching
snapshot before a new user request or API response. The unchanged system prompt
links those definitions to the initial request; their token counts remain estimates.
Visible source estimates are subtracted from the first-input estimate;
the remainder is `Unattributed context`.
Logs without these attachments retain the combined `System prompt & tools` estimate.
When recorded timing is available, `TTFT avg` shows the average time to first
token across completed turns with a TTFT observation, in seconds. Turns without
that observation are excluded; sessions without any observations omit the line.
Graph sections show each session's own average. Session stats JSON retains
`runtime.average_ttft_ms` in milliseconds; the API field remains
`runtime.average_time_to_first_token_ms`.
`TPS` shows output tokens divided by model-active seconds, excluding
tool execution but including prompt processing and waiting for output. It uses
`runtime.output_tokens_per_second`; Amp's available output estimate uses
`runtime.estimated_output_tokens_per_second` and is labeled `TPS (estimated)`.
Sessions without a rate omit the line; graph sections show each session's rate.
Claude TPS counts each provider response once, using its highest recorded
cumulative output count. Repeated stream records still contribute to recorded
token totals and cost estimates. Claude TPS is unavailable when response IDs
are missing from retained local evidence.
The `Recorded tokens` line shows `input`, `output`, `processed tokens`, and
`cache hit ratio`. The ratio is cached input divided by cache-inclusive input,
weighted by tokens across recorded usage entries. Cache writes count in the
denominator, not as hits. The ratio is unknown when input is zero or the required
counts are unavailable.
Input adds fresh input, cache reads, and cache writes across recorded usage entries,
so it has the same meaning for Codex and Claude. Output uses the recorded completion
count; processed tokens retains the canonical total, including separately counted
reasoning. Input is shown as unknown when the cache-inclusive count cannot be derived.
Use `session usage` or `session request-usage` for the individual cache and reasoning
buckets. JSON field names and recorded totals stay unchanged.
Stats also shows `Total cost` and `Cache savings` in USD. Total cost sums
request-level reported costs when available, otherwise token-price estimates.
Cache savings is an estimate of the same requests at ordinary input prices minus
their estimated cost with cache reads and writes. It includes cache-write premiums
and can be negative. Each request keeps its own model and input-length pricing tier;
output prices are unchanged in the comparison. Incomplete pricing shows as unknown.
JSON retains detailed token buckets and adds `cost_summary` with total cost,
net cache savings, no-cache cost, and request pricing coverage.
Claude writes use the recorded one-hour count when available; other writes use
the five-minute rate. See [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing#prompt-caching).
Estimates use standard token rates and recorded usage, rather than subscription
charges or an invoice; unrecorded service modifiers and server-tool fees are excluded.
Use `--details` to include estimated usage shares, provider input breakdowns,
message counts, and the compaction timeline:

```sh
uv run ct --source local session stats SESSION_ID --details
uv run ct --source local session graph stats SESSION_ID --details
```

`--details` changes the Markdown report only; JSON keeps the full existing data.
`session stats` always displays a compaction count, including zero. For Codex,
outer `compacted` records and legacy `context_compacted` events both establish
compaction boundaries. An event following a `compacted` record across only
context/usage metadata is paired with that record, not counted twice. Substantive
activity or lifecycle records end that pairing window. This also restores context
eviction accounting for newer rollouts that omit the legacy event.

Events contain normalized types, status, timestamps, and references, not raw
payloads. Item detail returns retained evidence, not an unrestricted raw log.
The removed `--include-content`, `--filter`, and `--drop-turns` flags are not supported.

`living` means the current turn is running. `not_living` does not mean the task
succeeded. Use `latest_turn_status` for observed turn completion or interruption.
See the [token glossary](token-usage-glossary.md) for measurement limits.

## Output and API calls

Report commands default to Markdown and accept `--output json`.
Items, events, and request usage are JSON-only. Put output and query flags after
the leaf command, as shown above.

`ct api call`, `batch`, and `schema` always return JSON. Dedicated command JSON
can use a compact CLI projection; API calls use the full public contract.
Inspect the schema before constructing a request:

```sh
uv run ct api schema session.items
uv run ct --source local api call session.items \
  --params '{"session_id":"SESSION_ID","limit":20}'
uv run ct --source local api call session.model_usage \
  --params '{"session_id":"SESSION_ID"}'
uv run ct --source local api call session.tool_usage \
  --params '{"session_id":"SESSION_ID","limit":20}'
```

Schemas require no discovery or credentials. Requests reject unknown fields.
Graph API methods require `root_session_id`. Session methods use `session_id`.
Model usage and tool usage have no dedicated CLI commands.
The API protocol remains `ct.api.v1`. Response metadata describes local live
retained evidence (`source: local`, `freshness: live`, `content_scope: retained`)
and contains no prepared-view identity. `living` change-feed behavior still uses
the existing SQLite stores in `~/.coding-trajectory/living-events/` and
`~/.coding-trajectory/living-sessions/`, pending the separate Job B living redesign.
Those stores have not migrated with Core's `local.sqlite` removal. Loop's
`investigations.sqlite3` and `monitor.sqlite3` remain product/user data, not Core
derived caches.

## Diagnostics and plugins

```sh
uv run ct doctor --since 7d
uv run ct doctor --since all --output json
uv run ct plugin list
uv run ct plugin loop web --help
```

Doctor checks Python and CLI versions, configuration, telemetry settings, vendor
roots, and local invocation telemetry. It no longer reads or reports `index.json`,
index-cache health, or stale cache-path mappings. Invocation failures, warning/info
recency, and latency trends remain available. It reports problems;
it does not repair them, delete data, or upload its report. Exit codes are 0 for healthy, 1 for
warnings or failed invocations, 2 for environment failure, and 3 for a corrupt log.

Telemetry stays in `~/.coding-trajectory/invocations.jsonl`. It records command
names, paths, timing, outcomes, and warnings, not arguments or transcript content.
Writes retain up to 30 days and 10 MiB. Corrupt records remain available for diagnosis.
Set `CT_TELEMETRY=0` to disable writes. This overrides `telemetry.enabled` in
`~/.coding-trajectory/config.toml`.

Plugin manifests declare entry points and required Core versions.
Plugins own their arguments, dependencies, and lifecycle. They must use public
Core contracts. See [Loop](loop-design.md) for the first-party browser workflow.
