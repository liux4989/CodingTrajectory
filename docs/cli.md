# CLI guide

Use `ct` to find a session, read its context, and inspect retained evidence.
Run examples from the repository root after `uv sync --all-packages --locked`.
Replace uppercase IDs and shell variables with values for your selected source.

## Select a source

Put `--source` and `--profile` before the command path:

```sh
uv run ct --source local project list
uv run ct --source shared --profile READER project list
uv run ct --source auto --profile READER project list
```

- `local` reads host-local sources without remote fallback.
- `shared` reads the configured remote workspace.
- `auto` tries local sources first. It uses remote reads only when local sources
  are unavailable or the target resource is missing.

A valid empty local result does not trigger fallback. Malformed evidence,
invalid requests, and ambiguous selection do not trigger fallback either.
The runtime does not merge local and remote evidence. Queries never publish data.

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
its ID. Remote IDs come from the workspace registry. Do not substitute IDs across sources.

Inventory, overview, search, and detail responses support pagination. Pass
`next_cursor` as `--cursor` and keep the other selectors unchanged.
API clients can use `view_manifest_sha256` to select an immutable prepared view.
Missing or expired views fail explicitly; do not silently continue on a newer view.

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
| `session stats SESSION_ID` | Time, status, counts, and context composition |
| `session usage SESSION_ID --turn TURN_ID` | Observed token buckets and cost evidence |
| `session request-usage SESSION_ID` | Provider-request ledger |
| `session graph overview SESSION_ID` | Orchestration topology |
| `session graph stats SESSION_ID` | Graph totals and per-session statistics |
| `session graph usage SESSION_ID` | Graph token accounting |

Session methods normally require an exact session ID. `turn_id` narrows that
session; it is not a replacement session selector. Tree and graph commands can
resolve an entry point in the same lineage or orchestration run.
Ordinary conversation forks do not enter the parent's graph aggregate.

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
Remote `session.search` and `living.events` are unavailable.

## Configure a connection

**Warning:** never put token values in command arguments, Git, reports, or browser bundles.
Inject the token through your host's secret store. For a headless reader:

```sh
uv run ct connection configure READER --role reader --default-source shared \
  --url "$CT_CLOUDFLARE_URL" --workspace-id "$CT_REMOTE_WORKSPACE_ID" \
  --token-env CT_ACCESS_TOKEN
uv run ct connection status READER
uv run ct connection check READER
```

Profiles store secret references, not token values. Interactive macOS setup uses
Keychain. `status` checks local configuration; `check` makes a read-only
authenticated request. A configured profile does not prove server access.

`rotate` replaces local credentials without changing collector identity or pending
work. `forget` removes local configuration; it does not revoke the server grant.
Server grants determine `read` and `collect` access, regardless of client role intent.
See [operations](operations.md) before configuring a collector or publishing data.

## Diagnostics and plugins

```sh
uv run ct doctor --since 7d
uv run ct doctor --since all --output json
uv run ct plugin list
uv run ct plugin loop web --help
```

Doctor checks the environment and local invocation telemetry. It reports problems;
it does not repair them or upload its report. Exit codes are 0 for healthy, 1 for
warnings or failed invocations, 2 for environment failure, and 3 for a corrupt log.

Telemetry stays in `~/.coding-trajectory/invocations.jsonl`. It records command
names, paths, timing, outcomes, and warnings, not arguments or transcript content.
Writes retain up to 30 days and 10 MiB. Corrupt records remain available for diagnosis.
Set `CT_TELEMETRY=0` to disable writes. This overrides `telemetry.enabled` in
`~/.coding-trajectory/config.toml`.

Plugin manifests declare entry points and required Core versions.
Plugins own their arguments, dependencies, and lifecycle. They must use public
Core contracts. See [Loop](loop-design.md) for the first-party browser workflow.
