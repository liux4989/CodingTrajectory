# Dashboard Refactor

## Goal

Turn the existing `cleanup` plugin into a **project/session management dashboard**.
`cleanup` stops being a top-level plugin and becomes one command inside the
dashboard. The dashboard ships as a manifest-backed executable plugin:

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
│ ct-plugin.json                                 │
│   name: dashboard                              │
│   command: ct-dashboard                        │
│   commands: ., projects, sessions, cleanup     │
╰───────────────────────┬───────────────────────╯
                        │
            ╭───────────▼────────────╮
            │ ct-dashboard           │
            │  registers local argv  │
            │  _load_projects()      │  calls ct project list --format json
            │  _load_sessions()      │  calls ct project sessions --format json
            │  DashboardApp (TUI)    │
            ╰───────────┬────────────╯
                        │
            ╭───────────▼────────────╮
            │ cleanup flow           │
            │  mounted under argv    │
            │  project/session tasks │  owned by executable
            ╰─────────────────────────╯
```

## Changes

### Cleanup
- Remove the standalone `cleanup` manifest.
- Mount cleanup subcommands under the dashboard executable's local argument
  parser.
- Keep cleanup policy, targets, actions, TUI, and renderers inside the
  executable package, not `coding_trajectory_cli`.

### Dashboard Executable
- Add a `dashboard` manifest whose command is `ct-dashboard`.
- `ct-dashboard` sets the bare-command TUI handler and registers `projects`,
  `sessions`, and `cleanup`.
- Shared loaders `_load_projects` / `_load_sessions` call `ct ... --data`
  command surfaces instead of importing CLI internals.
- `DashboardApp` (Textual) renders a project ListView + sessions DataTable, with
  `r` refresh / `q` quit. Gracefully degrades when Textual is absent.

### Manifest
- Swap the `cleanup` plugin manifest for `dashboard`.

## Data Sources

| Command   | Source command                  | Loader           |
|-----------|---------------------------------|------------------|
| projects  | `ct project list --format json`        | `_load_projects` |
| sessions  | `ct project sessions ... --format json` | `_load_sessions` |
| cleanup   | dashboard executable policy     | cleanup handlers |

## Follow-ups (not in this pass)

- Trigger cleanup directly from the TUI (action binding into the cleanup flow).
- Session drill-down (open `session overview` from the dashboard).
- Activity/usage columns in the project list.
