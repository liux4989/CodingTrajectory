# Plugin Design

## Goal

`ct` supports separately packaged command extensions without pushing that logic
into core discovery, ingestion, or canonical resource models.

The public namespace for these commands is `ct plugin ...`.

## Boundary

- First-party infrastructure stays under `ct project ...` and `ct session ...`.
- Plugin packages register commands under `ct plugin ...`.
- Plugins may call stable core helpers exposed by `coding_trajectory_cli`.
- Plugins should not patch core discovery, ingestion, or canonical models.

This keeps the core query surface stable while allowing package-specific command
packs to ship independently.

## Discovery

Plugins are loaded from the Python entry point group
`coding_trajectory.cli_plugins`.

Built-in inspection command:

- `ct plugin list` shows installed plugins and any load failures.

## Minimal Package Shape

```toml
[project]
name = "ct-export-pack"
dependencies = ["coding-trajectory"]

[project.entry-points."coding_trajectory.cli_plugins"]
export = "ct_export_pack.plugin:plugin"
```

## Minimal Plugin

```python
from __future__ import annotations

import argparse

from coding_trajectory_cli import CtPluginContext


class ExportPlugin:
    name = "export"

    def register(self, namespace_subparsers: argparse._SubParsersAction, ctx: CtPluginContext) -> None:
        export = namespace_subparsers.add_parser("export", help="Example plugin command.")
        export.add_argument("session_id", nargs="?")

        def handler(args: argparse.Namespace) -> dict:
            return ctx.dispatch_core(
                method="session.overview",
                params={"session_id": args.session_id} if args.session_id else {},
            )

        ctx.bind_command(export, handler=handler)


plugin = ExportPlugin()
```

## Plugin API

`coding_trajectory_cli` exports:

- `CtPluginContext`
- `CtCliPlugin`
- `PLUGIN_ENTRY_POINT_GROUP`

`CtPluginContext` provides:

- `bind_command(...)` to attach a handler and renderer to a plugin parser
- `dispatch_core(...)` to invoke first-party ct methods through the normal
  discovery and cache path
- `resolve_document_store(...)` when a plugin needs a resolved store without
  using the first-party dispatch table

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
  each plugin.

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
  classification should reuse canonical session metadata wherever possible.
- Permanent deletion should never be required for normal operation; archive or
  trash should be sufficient for routine cleanup.
