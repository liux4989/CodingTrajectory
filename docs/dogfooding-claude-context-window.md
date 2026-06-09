# Dogfooding Plan: Session Trajectory + Context Window

## Intent

Build and dogfood a CodingTrajectory plugin that explains any supported provider session as both:

- a trajectory timeline: user turns, assistant steps, tool calls, file/output effects, hooks, and sub-sessions;
- a context-window timeline: what entered context, when it entered, which category it belongs to, and the observed token impact.

The product reference is the Claude Code docs context-window explorer: category legend at the top, a left event stream with token deltas, and a right detail panel that can preview or pin an event. The goal is not to copy the docs page or limit the feature to Claude Code. The goal is to use CT's unified session APIs first. Provider-specific attribution belongs in CT core; the plugin should consume the normalized CT output instead of caring which provider stored which raw prompt details.

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

The plugin is a projection over these APIs. It should not rediscover sessions, parse provider logs directly, or own provider-specific evidence rules. If a provider needs richer attribution, that belongs in CT core so every command and dashboard route can reuse it.

The browser/dashboard view should be a new dashboard route for a selected session. From the dashboard sessions list, clicking a session should be able to route to a context-window page for that session. The page should mirror the docs interaction model:

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

Before implementation, run an evidence pass:

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

### Evidence Pass: 2026-06-09 Codex Sessions

Initial validation used these two real Codex sessions from this repo:

- `019eaa85-d49f-79a2-a68c-8ff699eb5292`
- `019eaa7f-4408-75b1-b8d7-d6d37679d495`

The current CT command surfaces were enough to build a v0 plugin projection for these sessions:

| Need | CT command | Result |
| --- | --- | --- |
| Session tree and turn activity | `ct session overview --global-scope --output json SESSION_ID` | Works for both sessions; exposes vendor, cwd, turns, user requests, activity labels, and step ids. |
| Context composition | `ct session stats --global-scope --output json SESSION_ID` | Works for both sessions; exposes model, context window, used tokens, runtime, messages, usage, quota, warnings, and nested context categories. |
| Turn-level usage/cost | `ct session usage --global-scope --output json SESSION_ID` | Works for both sessions; separates `tool_steps` from `response_steps` with token and cost totals. |
| Raw setup/context blocks | `ct session event-scan --global-scope SESSION_ID --type vendor.raw --filter raw_type=prompt_block --output json` | Works for both sessions; exposes source-labeled prompt blocks such as `base_instructions`, `permissions`, `collaboration_mode`, `skills_instructions`, `plugins_instructions`, developer blocks, and `agents_md`. |
| Raw token snapshots | `ct session event-scan --global-scope SESSION_ID --type vendor.raw --filter raw_type=token_count --output json` | Works for both sessions; exposes model, last usage, cumulative usage, context window, and quota snapshots. |
| Tool output evidence | `ct session event-scan --global-scope SESSION_ID --type tool.call.succeeded --output json` | Works for both sessions; exposes compact tool-result evidence with event ids, timestamps, tool call ids, exit codes, output lengths, and status. |

Observed session stats:

| Session | Provider | Turns | Tools | Latest context used | Main context-window signal |
| --- | --- | ---: | ---: | ---: | --- |
| `019eaa85-d49f-79a2-a68c-8ff699eb5292` | `codex_cli` | 6 | 43 | 86,679 tokens, 33.5% | `Tool results` are split into `Context gathered`, `Verification`, and `Repository operations`; `Context gathered` includes file reads, search results, and CLI/report inspection. |
| `019eaa7f-4408-75b1-b8d7-d6d37679d495` | `codex_cli` | 1 | 37 | 75,608 tokens, 29.3% | `Tool results` are split into `Context gathered`, `Code changes`, `Verification`, `Repository operations`, and `Other command output`. |

The newer `ct session stats` tool-output taxonomy directly helps this plugin. It gives the context-window view a useful first category tree without reading raw logs:

- `Context gathered`: read/search/list/report material that usually explains why context grows.
- `Code changes`: edits, writes, and formatter/fixer output.
- `Verification`: test, lint, build, and typecheck output.
- `Repository operations`: git and repository command output.
- `Dependency / environment`: package manager and environment diagnostics when present.
- `Execution / app runtime`: local app/server/runtime output when present.
- `External interaction`: network/deploy/remote command output when present.
- `Other command output` and `Other / unclassified`: retained fallbacks for commands or tools that should not be forced into the design taxonomy.

Decision from this pass:

- v0 can be built as a read-only plugin projection over current CT APIs for Codex sessions.
- No raw event enrichment is needed for these two Codex sessions.
- No near-term change is needed to expose internal category `confidence` or `source` fields in JSON.
- Cross-provider validation is still open: these two sessions validate Codex only, not Claude Code, Pi, or mixed-provider session graphs.

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

This slice should start from `ct session stats` categories and `ct session overview` timeline data. For Codex, the current stats taxonomy is already detailed enough to produce a useful v0 context-window report. Warnings must clearly say when a provider's category breakdown is approximate.

### Slice 2: Terminal Report

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

`ct session overview` answers "what happened in the session tree?" It shows sessions, turns, compact activity labels, and hierarchy. The terminal context-window report answers "what is occupying the model context, when did it enter, and how much did it cost?" It should link back to overview/event-detail ids instead of replacing those commands.

| Surface | Primary question | Shape | Intended use |
| --- | --- | --- | --- |
| `ct session overview` | What happened? | Session tree, turns, requests, compact activity labels, step ids. | Navigate the reconstructed trajectory and find relevant turns/steps. |
| Terminal context-window report | What is in context? | Context categories, token deltas, grouped timeline rows, warnings, and event/detail references. | Quickly diagnose context pressure and identify which kinds of input/output dominate. |

The terminal report should stay dense and scan-friendly. It should not try to show full source snippets, long tool outputs, hover previews, or all nested evidence. Those belong in JSON or the browser view.

### Slice 3: Browser/Dashboard View

Add a browser-friendly view after the JSON contract stabilizes. Reuse the docs interaction pattern:

- legend and stacked context bar;
- event stream grouped by `before_first_prompt`, `turn`, and `post_turn`;
- detail panel with source snippets, raw event ids, token fields, and confidence;
- keyboard and click selection;
- visible warnings for approximate attribution.

The browser view is not just a prettier terminal report. It is the interactive inspection surface:

| Surface | Primary question | Shape | Intended use |
| --- | --- | --- | --- |
| Terminal context-window report | What should I notice first? | Fixed text report with top categories, compact event rows, and warnings. | Fast CLI diagnosis, CI/log sharing, and agent-readable summaries. |
| Browser/dashboard context-window view | How do I inspect the evidence? | Visual context bar, selectable/pinnable events, detail panel, source snippets, and drilldowns. | Explore category composition, compare events, inspect evidence, and navigate without flooding stdout. |

The browser view should consume the same JSON contract as the terminal report. Differences should be presentation and interaction, not separate analysis logic.

### Deferred: Core Provider Attribution

Do not build a provider evidence adapter in the plugin. The Codex evidence pass showed current CT APIs are enough for the first plugin projection. If a future Claude Code, Pi, or mixed-provider evidence pass finds a concrete missing field, add provider-specific ingestion or service support in CT core. Keep canonical models agent-agnostic and expose the normalized result through the same CT APIs the plugin already consumes.

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
- Provider-specific attribution gaps are fixed in CT core, not in the plugin.

## Open Questions

- Can hook output be identified as context input, terminal output, or both?
- Are cached-token buckets enough to explain context pressure, or do we need per-message token attribution from raw records?
