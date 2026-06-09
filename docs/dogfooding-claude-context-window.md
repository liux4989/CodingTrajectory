# Dogfooding Plan: Claude Code Session Trajectory + Context Window

## Intent

Build and dogfood a CodingTrajectory plugin that explains a Claude Code session as both:

- a trajectory timeline: user turns, assistant steps, tool calls, file/output effects, hooks, and sub-sessions;
- a context-window timeline: what entered context, when it entered, which category it belongs to, and the observed token impact.

The product reference is the Claude Code docs context-window explorer: category legend at the top, a left event stream with token deltas, and a right detail panel that can preview or pin an event. The goal is not to copy the docs page. The goal is to use real Claude Code session logs to answer the same kind of questions for our own work.

## Product Shape

The first usable surface should be a plugin command, not a core command:

```text
ct plugin context-window SESSION_ID [--turn TURN_ID] [--output markdown|json]
```

Default output is compact and human-readable. JSON is the machine-readable escape hatch for dashboard/browser work.

The browser/dashboard view should mirror the docs interaction model:

- context categories shown as a stable legend;
- stacked window bar showing approximate category composition;
- chronological event list with token deltas;
- selectable event detail panel with source evidence, token accounting, and terminal visibility;
- pin behavior so a user can scroll the event list while keeping one detail open.

## Category Model

Use these category keys as the initial Claude Code projection vocabulary:

| Key | Meaning | Confidence target |
| --- | --- | --- |
| `system` | Claude Code system/developer prompt material | exact or inferred from raw prompt block |
| `claude_md` | user, project, and local `CLAUDE.md` content | exact when raw prompt block preserves source labels |
| `memory` | `MEMORY.md` and auto memory material | exact when source-labeled, otherwise inferred |
| `skills` | skill descriptions and loaded skill instructions | exact when source-labeled, otherwise inferred |
| `mcp` | MCP tool descriptions or deferred MCP metadata | exact when source-labeled, otherwise inferred |
| `rules` | explicit runtime rules, permissions, or policy-like blocks | inferred unless Claude exposes labels |
| `you` | user prompt text | exact from transcript |
| `files` | file contents attached or read into context | exact from tool/file events when available |
| `output` | tool results and command/browser output replayed into context | exact from tool result tokens when available |
| `claude` | assistant responses and reasoning that remain in history | exact from transcript, approximate for hidden reasoning |
| `hooks` | hook-generated context or terminal-visible hook output | exact when hook events exist |

Each event should carry both `category` and `confidence`. Avoid pretending Claude Code exposes Codex-style prompt-block attribution if the raw logs only expose cache buckets.

## Dogfood Sessions

Start with three real Claude Code sessions from this repo:

1. A short implementation session with a clear first prompt.
2. A tool-heavy session with shell output and file reads.
3. A long session that approaches cache/context pressure or has compaction-like behavior.

For each session, capture:

- raw Claude Code source path and session id;
- `ct session overview SESSION_ID --output json`;
- `ct session stats SESSION_ID --output json`;
- raw usage blocks containing `input_tokens`, `cache_creation_input_tokens`, `cached_input_tokens`, and cumulative input;
- any raw prompt/context blocks that preserve labels for `CLAUDE.md`, memory, skills, MCP, or environment info.

## Evidence Pass

Before implementation, run a one-day evidence pass:

1. Use `ct project sessions` to find candidate Claude Code sessions in this repo.
2. Inspect raw records for prompt-block labels, environment metadata, hook records, tool-use records, and usage blocks.
3. Produce a small matrix:
   - category;
   - available raw source;
   - exact token support;
   - fallback heuristic;
   - whether it appears in the terminal.
4. Decide which categories are supported in v0 and which must be marked approximate or unavailable.

The current `ct session stats` Claude implementation is cache-bucket based, so this evidence pass is mandatory before promising row-level categories like `Environment info +280`.

## Implementation Slices

### Slice 1: Read-Only Projection

Create a plugin-local projection module that calls stable CT JSON commands and emits:

```json
{
  "session_id": "...",
  "model": "...",
  "context_window_tokens": 200000,
  "used_tokens": 7800,
  "categories": [],
  "events": [],
  "warnings": []
}
```

This slice may use existing cache buckets only. Warnings must clearly say when the category breakdown is approximate.

### Slice 2: Raw Claude Evidence Adapter

If the evidence pass finds source-labeled prompt blocks, add the minimal ingestion/service support needed to preserve them as projection data. Keep canonical models agent-agnostic; store vendor-specific prompt/context source labels in vendor data or a projection layer.

### Slice 3: Terminal Report

Add a compact terminal report:

```text
# Claude Context Window

Model: claude-sonnet-... (200K context)
Used: 7.8K tokens, 18 events

Before first prompt
  system        +4.2K  System prompt
  memory          +680  Auto memory (MEMORY.md)
  environment     +280  Environment info

Turn 1
  you             +42  Fix the auth bug...
  output        +1.1K  shell: rg ...
```

Keep the report short. Full event data belongs in `--output json`.

### Slice 4: Browser/Dashboard View

Add a browser-friendly view after the JSON contract stabilizes. Reuse the docs interaction pattern:

- legend and stacked context bar;
- event stream grouped by `before_first_prompt`, `turn`, and `post_turn`;
- detail panel with source snippets, raw event ids, token fields, and confidence;
- keyboard and click selection;
- visible warnings for approximate attribution.

## Validation

Do not add unit tests for this task. Validate with real commands and dogfood artifacts:

```text
uv run ct plugin list
uv run ct session stats SESSION_ID --output json
uv run ct plugin context-window SESSION_ID
uv run ct plugin context-window SESSION_ID --output json
```

For the browser view, run the local server and inspect it through the in-app Browser at localhost. Verify at desktop and narrow widths that the event list and detail panel remain readable and no text overlaps.

## Acceptance Criteria

- The plugin can load at least one real Claude Code session from this repo.
- The report separates trajectory events from context-window categories.
- Every token number has a source and confidence value.
- Unsupported categories are explicit warnings, not silent omissions.
- The default output is readable for humans; JSON is stable enough for agents and the dashboard.
- Browser interaction supports hover/click preview and pinning a detail event.
- No core model changes are made until the raw Claude evidence pass proves they are needed.

## Open Questions

- Does Claude Code persist source-labeled prompt blocks for `CLAUDE.md`, memory, skills, MCP tools, and environment info, or only aggregate cache usage?
- Can hook output be identified as context input, terminal output, or both?
- Are cached-token buckets enough to explain context pressure, or do we need per-message token attribution from raw records?
- Should this plugin be a new `context-window` plugin or a dashboard route backed by a plugin-local command?
