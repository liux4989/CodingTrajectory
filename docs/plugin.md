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

### Proposed Command Shape

```text
ct plugin activity summary [--window 5h|today|72h|7d] [--project PROJECT] [--account ACCOUNT]
ct plugin activity sessions [--window 5h|today|72h|7d] [--project PROJECT] [--account ACCOUNT]
ct plugin activity usage [--window 5h|today|72h|7d] [--project PROJECT] [--account ACCOUNT]
```

Suggested behavior:

- `summary` returns aggregate counts and rollups for the selected scope.
- `sessions` lists matching sessions with timestamps, project, account, and
  compact activity labels.
- `usage` returns the session usage breakdown aggregated across matching
  sessions, reusing the same activity usage categories already exposed by
  `ct session usage`.

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
