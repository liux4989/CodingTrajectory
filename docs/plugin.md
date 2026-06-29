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

Plugins are dispatched from source. `ct` owns a command table that maps each
plugin name to its source directory and entry script:

```text
ct plugin NAME ...
```

resolves `NAME` to the built-in command table and runs the plugin's entry
script via `python <entry>.py <args>` with the working directory set to the
plugin's source directory. No registration, manifest files, or separate
installation step is required.

`ct plugin list` shows all available plugins and their entry points.

For local user-level publishing (installing `ct` as a global uv tool):

```text
scripts/publish-local.sh
```

The script rebuilds the global `coding-trajectory` uv tool from the current
checkout. Plugins are dispatched from source and do not require installation.

## Command Table

Each built-in plugin is defined by a `PluginCommand` entry in
`packages/cli/src/coding_trajectory_cli/plugins.py`:

- `name`: the namespace mounted under `ct plugin NAME`.
- `description`: one-line help text for `ct plugin list` and `ct plugin --help`.
- `dir`: plugin source directory relative to the workspace root.
- `entry`: entry script relative to the plugin directory.
- `requires_methods`: required service methods and minimum contract versions.
- `tools`: tool descriptors for help rendering.

Tool descriptors are optional and intentionally small:

- `name`: command segment under the plugin namespace, or `.` for the bare
  plugin command. Slash-delimited names such as `cleanup/cache` represent
  sub-level tools in one flat list.
- `summary`: one-line help text.

The command table is not an argparse schema. The plugin process owns its own
flags, validation, help text, runtime, local server lifecycle, and output.

## Executable Dispatch

`ct plugin NAME ...` resolves `NAME` to the built-in command table and runs the
plugin's entry script as a subprocess.

Dispatch rules:

- The plugin process receives the remaining command-line arguments unchanged.
- The subprocess working directory is the plugin's source directory.
- The subprocess inherits stdin, stdout, stderr, and the relevant `CT_*`
  environment.
- The subprocess exit status is the plugin exit status.
- `ct plugin NAME -h` is rendered by `ct` from the command table.
- Nested help such as `ct plugin NAME subcommand -h` is passed through to the
  plugin executable.

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
separate installation, manifest registration, or wheel packaging.

Plugin packages own their dependencies and do not import `coding_trajectory`
or `coding_trajectory_cli`.

For local user-level publishing (installing `ct` as a global uv tool):

```text
scripts/publish-local.sh
```

The script rebuilds the global `coding-trajectory` uv tool from the current
checkout. Plugins are dispatched from source and are not installed into the
tool environment.

## Activity Plugin

### Goal

Add an `activity` plugin that answers a higher-level operational question:
what happened across coding sessions, projects, and accounts during a bounded
time window.

This is intentionally a plugin concern rather than a first-party `ct session`
command because it is a cross-session analysis surface built on top of the
existing core session, overview, and usage data.

### Primary Use Cases

- Review recent activity for the last `5h`, `today`, `72h`, or `7d`.
- Break activity down by project.
- Break activity down by user account so multiple coding agents can be compared.
- Break activity down by session usage to understand token shape and
  log-reported cost where available.

### Command Shape

```text
ct plugin activity [--window 5h|today|72h|7d] [--project PROJECT] [--account ACCOUNT] [--extra-billing] [--format overview|json]
```

A single command emits one unified report for the selected scope:

- aggregate counts and usage totals for the window;
- activity usage broken down by category, reusing the categories already
  exposed by `ct session usage`;
- per-project rollups;
- matching sessions with timestamps, project, account, usage, and compact
  activity labels.

Use `--format json` inside the plugin surface, or `ct ... --output json` on core CLI commands, for exact structured payloads.

### Filter Model

- Time scope:
  - `5h` means rolling last 5 hours.
  - `today` means local calendar day in the reporting timezone.
  - `72h` means rolling last 72 hours.
  - `7d` means rolling last 7 days.
- Project scope:
  - Filter by canonical project name.
  - Omitted means all discovered projects.
- User scope:
  - Filter by account identity attached to the session.
  - Omitted means all accounts.

### Required Core Change: Account Identity

Project and time filtering can be composed from the current core model, but
user-account filtering requires a core ingestion change. We need account
information attached to the session record so the plugin does not infer user
identity from filesystem paths or vendor-specific metadata at query time.

Recommended direction:

- Add a stable `account` field to the canonical session metadata.
- Populate it during ingestion from the source event stream when the upstream
  vendor provides account identity.
- Preserve the original raw value separately if normalization is needed later.
- Treat missing account identity as `unknown` rather than fabricating a value.

Minimal canonical shape:

```json
{
  "account": {
    "key": "user@example.com",
    "label": "user@example.com",
    "vendor": "openai"
  }
}
```

`key` should be the stable filter value. `label` is for display. `vendor`
helps avoid collisions when different agent systems reuse the same username
string.

### Output Expectations

The plugin should keep stdout compact, consistent with the rest of `ct`:

- default text output for summaries and short listings
- `--format json` for downstream analysis
- usage output grouped by session and by activity category

The most useful top-level metrics are:

- sessions started
- turns observed
- active projects
- active accounts
- total tokens
- total cost
- usage totals by turn and tool-item stats

### Boundary

- Core remains responsible for canonical session metadata and usage analysis.
- The plugin remains responsible for multi-session filtering, aggregation, and
  presentation.
- Account identity should be added once in core, not rediscovered separately by
  each plugin. Executable plugins should consume that field from documented
  `ct` output.

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

The dashboard plugin also owns optional models.dev enrichment. It uses the
catalog for model context limits and estimated token prices in this projection.
Core metrics remain limited to values present in normalized session logs.

Plugin consumers may call `session.tool_usage` for estimated visible-content
tool input/output attribution and event-order diagnostics. Those item estimates
are cache-agnostic and must not replace observed session or turn usage totals.

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

## Review Plugin

The `review` plugin uses a Codex app-server LLM judge to analyze one coding
session for improvement opportunities:

```text
ct plugin review session SESSION_ID
ct plugin review session SESSION_ID --global-scope
ct plugin review session SESSION_ID --format json
ct plugin review session SESSION_ID --model gpt-5.5 --effort low
```

This is a plugin concern because it combines existing session projections into
an opinionated review report rather than adding new canonical ingestion data.
The plugin consumes documented machine-readable outputs:

- `ct session overview SESSION_ID --output json`
- `ct session stats SESSION_ID --output json`
- `ct session usage SESSION_ID --output json`

The plugin first builds a deterministic evidence packet from those sources:
metrics, compact tool activity, runtime stats, context composition, and usage
accounting. Findings and recommendations are judged by the Codex app-server by
default, using `CODEX_APP_SERVER_CMD` or `--app-server-cmd` to locate the
server command. `--judge deterministic` is available only as a local fallback
when the app-server is unavailable.

The default text report includes LLM-judged findings and recommendations. JSON
keeps the judge metadata, metrics, findings, and recommendations separate for
downstream analysis.

### Finding Scopes

Findings should distinguish who can act on the recommendation:

- `agent`: the coding agent can improve its workflow, such as batching related
  reads or narrowing commands.
- `environment`: the repo or project can provide better orientation, such as a
  repo map, architecture notes, or validation command recipes.
- `tooling`: the tools can reduce avoidable context pressure, such as compact
  output modes, capped listings, or machine-readable summaries.

### Evidence Model

The LLM judge receives observed session metrics rather than a fixed list of
expected problems. Initial evidence includes:

- context-gathering tokens from files read, search results, and file listings;
- tool-output tokens from the stats output bucket;
- turn-level usage and tool-item output signals from usage accounting;
- broad survey activity from overview tool counts;
- failed tool-call rate from runtime stats.

Recommendations must reference the finding and metrics that triggered them so
the report does not blame the agent for environment or tooling gaps.

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
