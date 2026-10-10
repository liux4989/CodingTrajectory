# Operations guide

Core reads local coding-agent logs. It does not publish data, manage remote
connections, or serve a remote API. Remote storage and deployment are deferred.
The parked Cloudflare tree is unsupported and is not built by Core CI.

## Capture Amp logs

The Amp project plugin is `.amp/plugins/coding-trajectory/index.ts`.
It writes private append-only journals to
`~/.coding-trajectory/amp/sessions/T-<thread-id>.jsonl`.
`CT_AMP_LOG_DIR` selects another directory.

**Warning:** the legacy plugin can still launch an external publication executable
after reconciliation and completed turns. Set `CT_AMP_AUTO_PUBLISH=0` before capture.
Legacy auto-publication is unsupported until deliberately redesigned. Do not
install or enable a collector for this local-only Core. Never upload raw journals.

Journals contain thread metadata, message revisions, and live observations.
Ingestion uses the latest revision for each stable ID. The plugin reconciles
full paged transcripts and records agent/tool hooks. An orb stores its own
journals; files do not synchronize between orbs.

Use only these versioned journals as Amp ingestion input. `amp threads export`
uses different IDs and is not interchangeable. Successful, matched live
`create_thread` evidence can establish a spawn. Read, message, and wait references
do not establish parentage. Local ingestion does not invent cross-host graph links.

Amp capture does not report provider tokens, billed cost, or exact inference
timing. Hook timestamps describe local observation. See the
[Amp output speed estimate](token-usage-glossary.md#amp-output-speed-estimate).

Offline capture qualification:

```sh
uv run python scripts/validate-amp-live.py
```

## Read local evidence

Install the locked workspace from the repository root:

```sh
uv sync --all-packages --frozen
uv run ct --source local project list
uv run ct --source local project sessions --project-id PROJECT_ID
```

Replace `PROJECT_ID` with an ID from the inventory. `auto` also selects local
sources; it never falls back to remote. If no supported source exists on the
host, queries report local source unavailability rather than reading a remote copy.
See [CLI](cli.md) for scoped detail queries and [Loop](loop-design.md) for browser access.

Inventory streams adapter-owned relationship metadata and discovers fresh topology
for each request. Detail queries ingest only selected runs and their canonical
dependencies, applying retention and redaction in memory. Reuse is limited to one
request or explicit batch; no topology or canonical graph is persisted across
requests, and Core no longer uses `~/.coding-trajectory/local.sqlite`. No upfront
preparation or manual publication is needed. Repeated reads of large runs repeat
ingestion, trading persistent-cache speed for stateless operation. Vendor logs
remain the evidence authority; this change does not delete logs or old databases.

Metrics are computed on request. Pricing keeps the existing live catalog path;
catalog estimates remain separate from provider-reported cost. Native metric
formulas and public vNext cursors are unchanged.

Living reads also write no derived state. `living.sessions` uses topology and
file metadata only; `living.events` loads the explicitly scoped retained run.
Old `local.sqlite`, `living-events/` and `living-sessions/` databases are no longer
read or updated. Moving these old derived stores to the Trash is a separate
post-acceptance cleanup, never a side effect of a query or migration. Loop's
`investigations.sqlite3` and `monitor.sqlite3` remain product/user data; do not
delete them as Core cache cleanup.

`living.sessions` returns header-level identities, project/cwd, source modified
time and size, `living`/`inactive` state and a digest. Global/project reads include
runs with a source modified in the last `horizon_days` (1–30, default 3 / 72
hours); explicit run scopes ignore the horizon. State uses each session's own
source mtime and a 300-second activity window. Changing time alone can therefore
change a digest.

`living.events` requires exactly one `root_session_id` or `session_id`; optional
`turn_id` and/or `item_id` narrow within that directly loaded run. A bare turn or
item ID is invalid; a cross-run narrowing ID returns `resource_not_found` without
loading any other run. Both modes return digests of retained details, so completing
a tool changes its digest even when the compact view hides those details. Compare
complete passes to discover removals; neither method emits historical deltas,
tombstones or reset operations.
Digest serialization is UTF-8 JSON with sorted keys, compact separators and
unescaped Unicode. Session digests exclude only their own `digest` field; event
digests cover the normalized details-mode `resource` payload.

Pages are count-bounded. Treat their unsigned query- and version-bound cursors
as opaque. Ordering is deterministic per call; a multi-page read is not a saved
snapshot of changing logs. Old published-view references are not supported.

## Validate local operation

Core CI runs Python/contract checks and the Loop web/integration checks. It does
not build, qualify, prepare, or deploy the parked remote runtime.

The local qualification checks admission parity for header-only sources, valid
runtime-only sources, and owned spawn/fork relationships. Codex fork admission
uses ingestion's parent-aware ownership cut, including segmented parent history:
inherited-only forks are absent until they have owned activity. Missing parents
retain the existing standalone behavior. These scans do not ingest transcripts.

```sh
uv run python scripts/check-core-protocol.py
uv run python scripts/validate-local-first-source-selection.py
uv run python scripts/benchmark-session-retrieval.py --command-activity-only --no-write
scripts/check-metrics-quality-gate.sh
uv run python scripts/validate-metrics-baselines.py
uv run python scripts/validate-amp-live.py
bun run --cwd packages/plugins/loop/web check
bun run --cwd packages/plugins/loop/web build
uv run python scripts/check-loop.py
```

The focused synthetic qualification covers command activity and retained replay.
The full retrieval benchmark (`--no-write` without `--command-activity-only`) is
diagnostic, not a CI gate: its pre-existing summary/search expectations do not
all hold on retained evidence. Neither workflow needs private logs or remote
credentials. Metric baselines use committed source evidence; do not replace
expected values with fresh output.
See [metrics validation](metrics-validation-quality-gate.md) for audit requirements.

## Release marker validation

[RELEASE.md](../RELEASE.md) retains the batch release marker. Advance `release_id`
by exactly one for an intentional reviewed batch. A target change requires that
increment. Release 0 remains the baseline.

```sh
uv run python scripts/check-release.py validate
uv run python scripts/check-release.py change --base-ref BASE --head-ref HEAD
```

Core CI validates marker transitions, but no longer prepares a remote candidate
or activates a Worker. The marker is not a deployment authorization or receipt.

## Prepare and deploy a release

Remote preparation and deployment are unsupported. Marker validation above is
the supported operation; there is no deployment procedure in this local-only Core.
