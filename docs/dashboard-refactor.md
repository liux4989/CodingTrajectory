# Dashboard Refactor

## Goal

Turn the existing `cleanup` plugin into a **project/session management dashboard**.
`cleanup` stops being a top-level plugin and becomes one command inside the
dashboard. The dashboard ships:

1. CLI subcommands that expose project/session data and actions.
2. An interactive Textual TUI (launched by the bare `dashboard` command) built
   on top of those same data handlers.

## Command Surface

Before:

```
ct plugin cleanup project [flags]
ct plugin cleanup session [flags]
```

After:

```
ct plugin dashboard                      # interactive TUI
ct plugin dashboard projects [flags]     # list managed projects
ct plugin dashboard sessions [PROJECT]   # list sessions for a project
ct plugin dashboard cleanup project ...  # migrated cleanup
ct plugin dashboard cleanup session ...  # migrated cleanup
```

- The dashboard subparsers are `required=False`. With no subcommand, the
  `dashboard` parser's default handler launches the TUI.
- `projects` / `sessions` are "dashboard commands for TUI use": the TUI consumes
  the same loader functions, and they double as plain CLI commands.

## Module Layout

```diagram
╭───────────────────────────────────────────────╮
│ pyproject.toml                                 │
│   entry point: dashboard -> dashboard:plugin   │
│   (cleanup entry point removed)                │
╰───────────────────────┬───────────────────────╯
                        │
            ╭───────────▼────────────╮
            │ builtins/dashboard.py  │
            │  DashboardPlugin       │  registers namespace + TUI
            │  _load_projects()      │  shared data loaders
            │  _load_sessions()      │
            │  DashboardApp (TUI)    │
            ╰───────────┬────────────╯
                        │ register_cleanup(sub, ctx)
            ╭───────────▼────────────╮
            │ builtins/cleanup.py    │
            │  register_cleanup(...)  │  was CleanupPlugin.register
            │  _handle_project/...    │  unchanged cleanup logic
            ╰─────────────────────────╯
```

## Changes

### `builtins/cleanup.py`
- Replace `class CleanupPlugin` / `CleanupPlugin.register` with a module-level
  `register_cleanup(parent_subparsers, ctx)` that mounts the `cleanup` parser and
  its `project` / `session` subcommands onto any subparsers action.
- Remove `plugin = CleanupPlugin()` (no longer an entry point).
- All cleanup logic (targets, actions, TUI, renderers) stays unchanged.

### `builtins/dashboard.py` (new)
- `DashboardPlugin.register` adds the `dashboard` namespace, sets the bare-command
  TUI handler, and registers `projects`, `sessions`, and `cleanup`
  (via `register_cleanup`).
- Shared loaders `_load_projects` / `_load_sessions` call `ctx.dispatch_core`
  (`project.list`, `project.sessions`).
- `DashboardApp` (Textual) renders a project ListView + sessions DataTable, with
  `r` refresh / `q` quit. Gracefully degrades when Textual is absent.

### `pyproject.toml`
- Swap the `cleanup` entry point for `dashboard`.

## Data Sources

| Command   | Core method        | Loader            |
|-----------|--------------------|-------------------|
| projects  | `project.list`     | `_load_projects`  |
| sessions  | `project.sessions` | `_load_sessions`  |
| cleanup   | (existing)         | cleanup handlers  |

## Follow-ups (not in this pass)

- Trigger cleanup directly from the TUI (action binding into the cleanup flow).
- Session drill-down (open `session overview` from the dashboard).
- Activity/usage columns in the project list.
```

