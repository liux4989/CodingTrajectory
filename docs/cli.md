# CLI Guide

The package publishes a `coding-trajectory` CLI for querying locally discovered
coding-agent logs.

## Discovery

Every command is stateless:

- default: scan only logs that match the current project
- `-g`: scan logs across all projects

The scan uses the known local vendor log homes:

- `~/.codex/sessions`
- `~/.claude/projects`
- `~/.gemini/tmp`
- `~/.local/share/amp/threads`

`list` commands print the discovered source files to stderr. `get` commands stay
quiet unless they fail.

## Usage

```bash
coding-trajectory trajectory list
coding-trajectory trajectory list -g
coding-trajectory session get c4ef6899-8915-4654-98ec-cd96fdc98969
coding-trajectory session get c4ef6899-8915-4654-98ec-cd96fdc98969 -g
coding-trajectory session list --trajectory-id 91c9f9f7-661a-50f8-93d6-5b516f91e8f5
coding-trajectory turn get 22222222-2222-2222-2222-222222222222 --view pretty
coding-trajectory event get 44444444-4444-4444-4444-444444444444 --json
coding-trajectory trajectory get 91c9f9f7-661a-50f8-93d6-5b516f91e8f5 --fields id,session_count
coding-trajectory trajectory list
coding-trajectory trajectory list -g
```

## Views

- `pretty`: curated JSON for humans and agents; for `session get`, includes a
  concise ordered mixed timeline. `payload_preview` entries may backfill related
  labels from other events in the same session.
- `raw`: canonical JSON payload with no projection

For the display contract and field-level output rules, see
[`docs/cli-output-spec.md`](./cli-output-spec.md).
