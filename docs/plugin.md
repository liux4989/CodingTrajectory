# Plugin Design

## Goal

`ct` supports separately packaged command extensions without pushing that logic
into core discovery, ingestion, or canonical resource models.

The public namespace for these commands is `ct plugin ...`.

## Boundary

- First-party infrastructure stays under `ct project ...` and `ct session ...`.
- Plugin manifests register commands under `ct plugin ...`.
- Plugins run as separate executables. They do not import `coding_trajectory`
  or `coding_trajectory_cli`.
- Plugins may call the `ct` CLI or read documented machine-readable outputs.
- Plugins should not patch core discovery, ingestion, or canonical models.

This keeps the core query surface stable while allowing package-specific command
packs to ship independently.

## Dispatch

Plugins are dispatched from source. `ct` discovers plugins by scanning the
plugin root for `plugin.toml` manifests (see _Plugin Manifest_), then resolves
`ct plugin NAME ...` to the plugin's entry script and runs it via
`python <entry>.py <args>` with the working directory set to the plugin's
source directory. No core code edits, registration, or separate installation
step is required to add a plugin: drop a directory containing a
`plugin.toml` under the plugin root.

```text
ct plugin NAME ...
```

`ct plugin list` shows all discovered plugins, their entry points, required
ct methods, and any compatibility errors.

For local user-level publishing (installing `ct` as a global uv tool):

```text
scripts/publish-local.sh
```

The script rebuilds the global `coding-trajectory` uv tool from the current
checkout. Plugins are dispatched from source and do not require installation.

### Plugin root resolution

`ct` resolves the plugin root in this order:

1. `CT_PLUGIN_DIR` environment variable (absolute path).
2. The `packages/plugins` directory located next to the CLI package source
   (the checkout layout under `[repo]/packages/plugins`).

The editable install from `scripts/publish-local.sh` keeps `ct` tied to the
source checkout, so the default resolution points at the live plugin source.

## Plugin Manifest

Each plugin ships a `plugin.toml` in its source directory. Fields:

- `name`: the namespace mounted under `ct plugin NAME`. Must not be `list`.
- `description`: one-line help text for `ct plugin list` and the `ct plugin`
  index.
- `entry`: entry script relative to the plugin directory.
- `requires`: optional table of `"service.method" = min_version` entries;
  `ct` checks these against `SERVICE_CONTRACTS` before invoking the plugin.
- `tools`: optional array of `[[tools]]` tables with `name` and `summary`.
  Tool names are descriptive only (`.` for the bare plugin command;
  slash-delimited names such as `session/context-window` describe nested
  tools in one flat list). Tool summaries may lag the executable's own `-h`.

The manifest is metadata for `ct plugin list` and the `ct plugin` index, not
an argparse schema. The plugin process owns its own flags, validation, help
text, runtime, local server lifecycle, and output.

## Executable Dispatch

`ct plugin NAME ...` resolves `NAME` against the discovered manifest table and
runs the plugin's entry script as a subprocess.

Dispatch rules:

- Before invoking, `ct` runs the `requires` contract preflight and checks the
  entry script exists; failures are reported as structured errors without
  spawning the plugin.
- The plugin process receives the remaining command-line arguments unchanged.
- The subprocess working directory is the plugin's source directory.
- The subprocess inherits stdin, stdout, stderr, and the relevant `CT_*`
  environment.
- The subprocess exit status is the plugin exit status.
- All help is forwarded to the plugin executable: `ct plugin NAME -h`,
  `ct plugin NAME sub -h`, and any deeper `... -h` are passed through
  unchanged so the plugin owns its full help text. Core renders only the
  brief `ct plugin` index (names + descriptions).

Example:

```text
ct plugin dashboard session abc123
```

Dispatches as:

```text
cd packages/plugins/dashboard && python dashboard.py session abc123
```

Plugins that need first-party one-shot reports should call stable CLI surfaces,
preferably with machine-readable output:

```text
ct session overview abc123 --output json
ct project list --output json
```

Plugins that need cross-session or multi-method automation should use the
structured service API instead of composing human command output:

```text
ct api call session.usage --params '{"session_id":"abc123"}'
ct api batch --requests '[{"id":"usage-1","method":"session.usage","params":{"session_id":"abc123"}}]'
ct api call project.sessions --global-scope --params '{"since_days":30,"include":["runtime","usage"]}'
```

Use `call` for one cohesive query and `batch` for independent queries that
should share one runtime invocation. Keep dependent orchestration in the
plugin. Standard JSON tools remain useful outside the API boundary:

```text
ct api call project.sessions --global-scope \
  --params '{"since_days":30,"include":["runtime","usage"]}' |
  jq '.result.items | group_by(.project)'
```

Every service method exposes its versioned request and response contract
without performing discovery or ingestion:

```text
ct api schema session.usage
ct api schema project.list
```

Schemas are generated from the Pydantic models used for runtime validation.

There is no compatibility layer for the old in-process Python plugin API.

## Distribution

First-party plugins live under `packages/plugins/` as workspace members.
They are dispatched from source by `ct plugin NAME ...` and do not require
separate installation or wheel packaging. Each plugin advertises itself with
a `plugin.toml` manifest in its source directory; `ct` discovers plugins by
scanning the plugin root rather than maintaining a built-in command table.

Plugin packages own their dependencies and do not import `coding_trajectory`
or `coding_trajectory_cli`.

For local user-level publishing (installing `ct` as a global uv tool):

```text
scripts/publish-local.sh
```

The script rebuilds the global `coding-trajectory` uv tool from the current
checkout. Plugins are dispatched from source and are not installed into the
tool environment.

## Dashboard Context Window

The dashboard plugin's `session context-window` subcommand projects the existing
session overview, stats, and usage JSON surfaces into one context-composition
report:

```text
ct plugin dashboard session context-window SESSION_ID
ct plugin dashboard session context-window SESSION_ID --turn TURN_ID
ct plugin dashboard session context-window SESSION_ID --output json
```

The default report is compact text. JSON preserves token evidence as an object
with `value`, `confidence`, and `source`, so dashboards and agents do not need
to infer whether a number is exact or estimated.

Core owns optional models.dev enrichment and emits request-summed price
evidence next to normalized usage. The dashboard consumes that evidence rather
than repricing aggregate turn or session buckets.

Plugin consumers may call `session.tool_usage` for estimated visible-content
tool input/output attribution and event-order diagnostics. Allocated real-token
cost is bounded to the containing turn and must not replace observed session or
turn usage totals.

Provider behavior remains explicit:

- Semantic categories measure observed canonical content and are not scaled to
  the provider-reported active context total.
- Exact provider cache/input buckets are exposed separately as
  `provider_usage_buckets`.
- Shell reads, searches, and listings reuse core item-analysis semantics.
  Commands and uncommon tools fall back to the common `Output` category; deeper
  command interpretation belongs in session overview analysis.
- User and assistant timeline deltas estimate visible overview text.
- Tool timeline rows do not invent token deltas when result text is unavailable.

The dashboard session list links to
`/sessions/$sessionId/context-window`. That route consumes the plugin JSON
through `/api/sessions/context-window` and adds the composition bar, event
selection, hover preview, and pinned detail behavior.

## Dashboard Cleanup

### Goal

Add cleanup operations to the `dashboard` plugin so destructive actions live
under the dashboard's `project` and `session` command families. The first
version should stay narrow: clean up old projects and clean up empty session
logs.

This stays a plugin concern rather than a first-party `ct project` or
`ct session` command because it performs destructive filesystem actions and
uses local policy about what is safe to delete.

### Primary Use Cases

- Preview old projects that are likely safe to remove.
- Remove empty session logs that contain no useful turns or events.
- Keep the Codex and Pi session stores small enough for fast discovery.
- Produce an auditable cleanup report before and after deletion.

### Command Shape

```text
ct plugin dashboard project [--agent-vendor VENDOR]
ct plugin dashboard web [--host 127.0.0.1] [--port 8765] [--open]
ct plugin dashboard benchmark [--fixture-file FILE] [--repeat N] [--api NAME] [--compare REPORT] [--save REPORT]
ct plugin dashboard project cleanup [--dry-run] [--older-than 30d] [--path PATH] [--detail]
ct plugin dashboard session [PROJECT] [--since-days N|--all-time] [--agent-vendor VENDOR]
ct plugin dashboard session cleanup [--agent-vendor codex|pi] [--trash|--delete] [--confirm]
```

Project cleanup behavior:

- Running project cleanup without flags permanently deletes every matching
  candidate.
- `--dry-run` lists matching candidates without changing the filesystem.
- `--detail` prints the exact JSON payload for either mode.
- `dashboard project` reads candidates from the global `ct project list` result.
- Project cleanup supports permanent deletion only; it does not expose trash or
  terminal UI modes.
- The web dashboard provides target selection and confirmation for interactive
  project cleanup.

The first command surface should remain this small. More complex cleanup flows
should move into the dashboard-owned web application rather than adding many
one-off flags to the CLI.

### Project Cleanup Rules

Project candidates should be selected from discovered project metadata and
filesystem inspection, not from name matching alone. The first version only
needs to classify projects that are older than the configured retention window.

Recommended candidate signals:

- project has no sessions newer than `--older-than`;
- project directory is under a configured cleanup root;
- project has no uncommitted git changes;
- project has no unpushed commits on the current branch;
- project has no active lock file or running dev-server marker recognized by a
  local policy file.

Recommended exclusions:

- current working directory and its parents;
- git repositories with dirty working trees;
- repositories with local commits that are not reachable from a configured
  remote;
- directories outside explicitly allowed cleanup roots;
- directories with a local `.ct-cleanup-keep` marker.

### Session Cleanup Rules

Session cleanup should classify logs before removing anything. The first
version only needs to remove empty sessions.

Recommended candidate classes:

- `empty`: session file has metadata but no user-visible turns or useful
  events.

Recommended exclusions:

- sessions modified in the last 24 hours;
- sessions connected to a non-empty parent or child session tree;
- sessions that contain failed, interrupted, or in-progress status;
- sessions with a local keep marker in companion metadata.

### Interactive Workflow

The full cleanup workflow should be exposed through an interactive CLI flow
instead of a large flag surface. The interactive flow can guide the user through discovery,
review, selection, and execution while keeping the simple CLI useful for
automation.

The dashboard also provides a plugin-local web program:

- `ct plugin dashboard web` starts a local HTTP server for the built React
  dashboard.
- The Python server exposes dashboard-owned JSON endpoints and serves the built
  frontend from `packages/plugins/dashboard/web/dist`.
- `ct plugin dashboard benchmark` invokes the read-only service routes directly,
  measures dashboard-cache-cold and warm latency, attributes latency to nested
  `ct` calls, and hashes deterministic responses. Reports are written only with
  an explicit `--save`; the historical all-route baseline is protected from
  partial or unsuccessful replacement. Long-running token-efficiency worker
  projections are opt-in with `--include-expensive`.
- Pinned fixture manifests set the route set, minimum repeats, thresholds, and
  expected response digests. Strict `--compare` mode rejects incompatible or
  incomplete evidence and exits nonzero for response drift, latency regression,
  or warning/critical threshold breaches. These manifests pin a workload on the
  current host; the response digest detects source drift, but the underlying
  session corpus is not bundled for cross-machine reconstruction.
- `GET /api/diagnostics/cache` exposes aggregate projection/source cache
  counters and gauges without exposing cache keys or session identifiers.

The checked-in `benchmarks/results/dashboard-api-baseline.json` is the
historical pre-optimization all-route scan. Its 30-second timeouts are censored
failures, not comparable latency samples. The repeat-five
`dashboard-context-window-before-v1.json` is a legacy same-host control trace;
because schema v1 lacks source and response provenance, it remains observational
and is intentionally rejected by strict schema-v2 comparison. Candidate evidence
is generated separately so a targeted run cannot overwrite either history:

```bash
CT_COMMAND="$PWD/.venv/bin/ct" uv run ct plugin dashboard benchmark \
  --fixture-file benchmarks/fixtures/dashboard-context-window-large-v1.json \
  --save benchmarks/results/dashboard-context-window-after-v2.json \
  --json
```

The artifact is written before the command returns its gate status, so a valid
measurement remains inspectable even when the configured latency threshold
causes a nonzero exit.
- The frontend package lives under `packages/plugins/dashboard/web` and uses
  React, TanStack Query, TanStack Router, and local shadcn-style UI primitives.
- Cleanup actions remain preview-first and POST-only; the web UI sends explicit
  selected paths and action names before the backend calls the existing
  plugin-local cleanup functions.

Expected interactive flow:

- scan old project candidates and empty session candidates;
- show grouped candidates with paths, ages, sizes, and skip reasons;
- let the user expand skipped categories and inspect the paths in each group;
- let the user select or deselect candidates;
- default to trash or archive actions;
- require explicit confirmation before deletion;
- write the same cleanup manifest as the CLI.

### Safety Model

The plugin should make cleanup safe by default:

- Always compute candidates before taking action.
- Print counts, total bytes, and representative paths in overview mode.
- Require `--confirm` or equivalent explicit acknowledgement for non-dry-run
  actions if the command is run interactively.
- Prefer moving files to the operating-system trash or a configured archive
  directory before permanent deletion.
- Write a machine-readable cleanup manifest for every non-dry-run action.
- Treat unreadable files, parse failures, and unknown vendor formats as
  skipped, not deleted.

Example manifest shape:

```json
{
  "action": "trash",
  "dry_run": false,
  "generated_at": "2026-06-05T12:00:00Z",
  "projects": [
    {
      "path": "/tmp/example-project",
      "reason": ["older_than_retention", "temporary_root"],
      "bytes": 120034
    }
  ],
  "sessions": [
    {
      "path": "~/.codex/sessions/2026/05/old-session.jsonl",
      "vendor": "codex",
      "class": "empty",
      "bytes": 930
    }
  ]
}
```

### Output Expectations

Default text output should stay compact:

- total project candidates and bytes reclaimable;
- total session candidates and bytes reclaimable;
- skipped counts grouped by reason;
- action mode: dry run, trash, archive, or delete;
- path to the cleanup manifest for non-dry-run actions.

Default text output stays compact; use `--detail` or underlying core `ct ... --output json`
commands when you need the full candidate list and skip reasons.

### Boundary

- Core remains responsible for discovering projects and parsing sessions into
  canonical metadata.
- The plugin remains responsible for cleanup policy, safety checks, candidate
  classification, and filesystem actions.
- Vendor-specific deletion paths should live in the plugin, but candidate
  classification should reuse canonical session metadata from documented `ct`
  output wherever possible.
- Permanent deletion should never be required for normal operation; archive or
  trash should be sufficient for routine cleanup.
