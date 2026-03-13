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
coding-trajectory turn get 22222222-2222-2222-2222-222222222222 --view summary
coding-trajectory event get 44444444-4444-4444-4444-444444444444 --json
coding-trajectory trajectory get 91c9f9f7-661a-50f8-93d6-5b516f91e8f5 --fields id,session_count
coding-trajectory trajectory list --view summary
coding-trajectory trajectory list -g --view summary
```

## Views

- `summary`: compact JSON for humans and lightweight scripting; for
  `session get`, this includes a concise ordered mixed timeline of standalone
  session events and turn refs. Use `turn get` to inspect a full turn item.
  Timeline `payload_preview` entries may backfill related labels such as
  `tool_name` or `model` from other events in the same session when the raw
  event only carries `tool_call_id` or `request_id`
- `pretty`: terminal-oriented text output
- `raw`: canonical JSON payload with no projection

For the display contract and field-level output rules, see
[`docs/cli-output-spec.md`](./cli-output-spec.md).
