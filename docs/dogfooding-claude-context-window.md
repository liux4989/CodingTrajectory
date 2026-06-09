# Dogfooding Plan: Session Trajectory + Context Window

## Intent

Build and dogfood a CodingTrajectory plugin that explains any supported provider session as both:

- a trajectory timeline: user turns, assistant steps, tool calls, file/output effects, hooks, and sub-sessions;
- a context-window timeline: what entered context, when it entered, which category it belongs to, and the observed token impact.

The product reference is the Claude Code docs context-window explorer: category legend at the top, a left event stream with token deltas, and a right detail panel that can preview or pin an event. The goal is not to copy the docs page or limit the feature to Claude Code. The goal is to use CT's unified session APIs first, then add provider-specific enrichment only where the unified model cannot yet answer context-window questions.

## Product Shape

The first usable surface should be a plugin command, not a core command:

```text
ct plugin context-window SESSION_ID [--turn TURN_ID] [--output markdown|json]
```

Default output is compact and human-readable. JSON is the machine-readable escape hatch for dashboard/browser work.

The plugin should use the existing CT commands as its source of truth:

- `ct session overview` for normalized trajectory structure;
- `ct session stats` for provider-normalized context/token composition;
- `ct session usage` for turn-level token and cost accounting;
- `ct session step-detail`, `ct session event-detail`, and `ct session event-scan` for focused evidence lookups.

The plugin is a projection over these APIs. It should not rediscover sessions or parse provider logs directly unless the current CT APIs are missing a specific evidence field.

The browser/dashboard view should mirror the docs interaction model:

- context categories shown as a stable legend;
- stacked window bar showing approximate category composition;
- chronological event list with token deltas;
- selectable event detail panel with source evidence, token accounting, and terminal visibility;
- pin behavior so a user can scroll the event list while keeping one detail open.

## Category Model

Use these category keys as the cross-provider projection vocabulary. Provider adapters may map these categories from different sources and with different confidence levels:

| Key | Meaning | Confidence target |
| --- | --- | --- |
| `system` | system/developer prompt material | exact or inferred from provider prompt blocks |
| `project_instructions` | provider-specific project instructions such as `CLAUDE.md`, `AGENTS.md`, or local rules | exact when source-labeled, otherwise inferred |
| `memory` | durable or automatic memory material | exact when source-labeled, otherwise inferred |
| `skills` | skill descriptions and loaded skill instructions | exact when source-labeled, otherwise inferred |
| `mcp` | MCP tool descriptions or deferred MCP metadata | exact when source-labeled, otherwise inferred |
| `rules` | explicit runtime rules, permissions, or policy-like blocks | inferred unless the provider exposes labels |
| `you` | user prompt text | exact from transcript |
| `files` | file contents attached or read into context | exact from tool/file events when available |
| `output` | tool results and command/browser output replayed into context | exact from tool result tokens when available |
| `assistant` | assistant responses and reasoning that remain in history | exact from transcript, approximate for hidden reasoning |
| `hooks` | hook-generated context or terminal-visible hook output | exact when hook events exist |

Each event should carry both `category` and `confidence`. Avoid pretending every provider exposes the same attribution. For example, Claude Code may expose cache buckets while Codex may expose richer prompt-block categories.

## Dogfood Sessions

Start with real sessions from multiple providers, using Claude Code as the visual reference case:

1. A short Claude Code implementation session with a clear first prompt.
2. A tool-heavy Codex session with shell output, file reads, and browser output.
3. A long session from any supported provider that approaches cache/context pressure or has compaction-like behavior.

For each session, capture:

- provider, source path, and session id;
- `ct session overview SESSION_ID --output json`;
- `ct session stats SESSION_ID --output json`;
- `ct session usage SESSION_ID --output json`;
- focused detail output for the events that appear in the context-window report;
- any provider raw prompt/context blocks that preserve labels for project instructions, memory, skills, MCP, or environment info.

## Evidence Pass

Before implementation, run a one-day evidence pass:

1. Use `ct project sessions` to find candidate sessions in this repo across supported providers.
2. Use current CT JSON commands first, then inspect raw records only for missing prompt-block labels, environment metadata, hook records, tool-use records, and usage blocks.
3. Produce a small matrix:
   - provider;
   - category;
   - available CT API source;
   - available raw fallback source;
   - exact token support;
   - fallback heuristic;
   - whether it appears in the terminal.
4. Decide which categories are supported in v0 and which must be marked approximate or unavailable.

Current CT commands do provide the base information needed for the plugin: normalized trajectory, usage, stats, and event detail. The evidence pass is about completeness of category attribution, not about whether a plugin can be built. For example, if a provider only exposes aggregate cache buckets, row-level categories like `Environment info +280` must be approximate or unavailable for that provider.

## Implementation Slices

### Slice 1: Read-Only Projection

Create a plugin-local projection module that calls stable CT JSON commands and emits:

```json
{
  "session_id": "...",
  "vendor": "...",
  "model": "...",
  "context_window_tokens": 200000,
  "used_tokens": 7800,
  "categories": [],
  "events": [],
  "warnings": []
}
```

This slice may use existing cache buckets only. Warnings must clearly say when the category breakdown is approximate.

### Slice 2: Provider Evidence Adapter

If the evidence pass finds source-labeled prompt blocks that are not exposed through CT APIs, add the minimal ingestion/service support needed to preserve them as projection data. Keep canonical models agent-agnostic; store provider-specific prompt/context source labels in vendor data or a projection layer.

### Slice 3: Terminal Report

Add a compact terminal report that differs from `ct session overview` by focusing on context composition rather than hierarchy:

```text
# Context Window

Provider: claude_code
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

`ct session overview` answers "what happened in the session tree?" It shows sessions, turns, compact activity labels, and hierarchy. The proposed context-window report answers "what is occupying the model context, when did it enter, and how much did it cost?" It should link back to overview/event-detail ids instead of replacing those commands.

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
- The plugin can load at least one non-Claude provider session through the same CT command path.
- The report separates trajectory events from context-window categories.
- Every token number has a source and confidence value.
- Unsupported categories are explicit warnings, not silent omissions.
- The default output is readable for humans; JSON is stable enough for agents and the dashboard.
- Browser interaction supports hover/click preview and pinning a detail event.
- No core model changes are made until the provider evidence pass proves they are needed.

## Open Questions

- Which providers persist source-labeled prompt blocks for project instructions, memory, skills, MCP tools, and environment info, and which only expose aggregate usage?
- Can hook output be identified as context input, terminal output, or both?
- Are cached-token buckets enough to explain context pressure, or do we need per-message token attribution from raw records?
- Should this plugin be a new `context-window` plugin or a dashboard route backed by a plugin-local command?
