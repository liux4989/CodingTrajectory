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

## Discovery

Plugins are discovered from manifest files, not Python entry points.

Supported manifest locations:

- directories listed in `CT_PLUGIN_MANIFEST_PATH`, separated by the platform
  path separator;
- repository built-in plugin manifests under `plugins/*/ct-plugin.json`
  beside this repo, when available from the installed source path;
- project-level manifests under `.ct/plugins/`;
- user-level manifests under `~/.ct/plugins/`.

Each manifest describes one public plugin namespace. The CLI validates manifests
before exposing commands.

Built-in plugins for this repository live under `plugins/<name>/` with their
manifest and executable together. They use the same manifest and subprocess
contract as third-party plugins.

Built-in inspection command:

- `ct plugin list` shows discovered manifests and validation failures.

## Minimal Manifest

```json
{
  "schemaVersion": 1,
  "name": "export",
  "version": "0.1.0",
  "requiresCt": ">=0.1.0",
  "description": "Export ct session data.",
  "command": "ct-export",
  "commands": [
    {
      "name": "session",
      "summary": "Export one session."
    }
  ],
  "capabilities": ["session.read"]
}
```

Required fields:

- `schemaVersion`: currently `1`.
- `name`: the namespace mounted under `ct plugin NAME`.
- `version`: plugin version.
- `description`: one-line help text for `ct plugin list` and `ct plugin --help`.
- `command`: command name or path to execute.

Optional fields:

- `requiresCt`: version requirement for the installed `ct` command.
- `commands`: command descriptors for manifest-rendered help.
- `capabilities`: short data/action labels such as `project.read` or
  `session.read`.

Command descriptors are optional and intentionally small:

- `name`: command segment under the plugin namespace, or `.` for the bare
  plugin command.
- `summary`: one-line help text.

The manifest is not an argparse schema. The executable owns its own flags,
validation, help text, and output.

## Executable Dispatch

`ct plugin NAME ...` resolves `NAME` to a validated manifest and starts the
manifest executable as a subprocess.

Dispatch rules:

- The executable receives the remaining command-line arguments unchanged.
- The subprocess working directory is the caller's current directory.
- The subprocess inherits stdin, stdout, stderr, and the relevant `CT_*`
  environment.
- The subprocess exit status is the plugin exit status.
- `--help` after the plugin namespace is rendered by `ct` from the manifest.

Example:

```text
ct plugin export session abc123 --format json
```

Dispatches as:

```text
ct-export session abc123 --format json
```

Plugins that need first-party data should call stable CLI surfaces, preferably
with machine-readable output:

```text
ct session overview abc123 --data
ct project list --format json
```

There is no compatibility layer for the old in-process Python plugin API.

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
- Break activity down by session usage to understand token and cost shape.

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

Use `--format json` (or `--output FILE`) for the exact structured payload.

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
- usage split by `tool_steps`, `response_steps`, `mixed_steps`, and
  `other_steps`

### Boundary

- Core remains responsible for canonical session metadata and usage analysis.
- The plugin remains responsible for multi-session filtering, aggregation, and
  presentation.
- Account identity should be added once in core, not rediscovered separately by
  each plugin. Executable plugins should consume that field from documented
  `ct` output.

## Cleanup Plugin

### Goal

Add a `cleanup` plugin that finds and removes low-value artifacts created by
coding agents. The first version should stay narrow: clean up old projects and
clean up empty session logs.

This is intentionally a plugin concern rather than a first-party `ct project`
or `ct session` command because it performs destructive filesystem actions and
uses local policy about what is safe to delete.

### Primary Use Cases

- Preview old projects that are likely safe to remove.
- Remove empty session logs that contain no useful turns or events.
- Keep the Codex and Pi session stores small enough for fast discovery.
- Produce an auditable cleanup report before and after deletion.

### Command Shape

```text
ct plugin cleanup project [--older-than 30d] [--path PATH] [--trash|--delete] [--confirm] [--detail]
ct plugin cleanup session [--agent-vendor codex|pi] [--trash|--delete] [--confirm] [--detail]
```

Default behavior should be conservative:

- Running without `--trash` or `--delete` opens the interactive cleanup flow.
- `cleanup project` reads candidates from the global `ct project list` result.
- `--delete` requires an explicit flag and should not be implied by any
  shorthand command.
- `--trash` and `--delete` require `--confirm`.
- Non-interactive action flags operate on all matching candidates.

The first command surface should remain this small. More complex cleanup flows
should move into the interactive TUI rather than adding many one-off flags to
the CLI.

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

### TUI Workflow

The full cleanup workflow should be exposed through an interactive TUI instead
of a large flag surface. The TUI can guide the user through discovery, review,
selection, and execution while keeping the simple CLI useful for automation.

Expected TUI flow:

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

Use `--detail` or `--output FILE` for the full candidate list and skip
reasons.

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
