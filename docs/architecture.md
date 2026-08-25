# coding-trajectory — Project Specification

## Overview

**coding-trajectory** is a unified canonical model layer and CLI tooling for reconstructing, querying, and analyzing coding-agent session graphs. It normalizes raw vendor logs from multiple coding agents (Codex CLI, Claude Code, Pi) into a single agent-agnostic hierarchy, enabling cross-vendor analysis, token accounting, and session replay.

- **Author:** liuxinjia
- **License:** Private
- **Repository:** coding-trajectory (local workspace)

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CLI (`ct`)                          │  Plugins                         │
│  ┌─────────────┐ ┌──────────────┐    │  ┌──────┐ ┌───────┐             │
│  │ project     │ │ session      │    │  │ data │ │ code- │             │
│  │ list/sess.  │ │ over./stats/ │    │  │ hub  │ │ time  │             │
│  │             │ │ usage/item   │    │  └───┬──┘ └───┬───┘             │
└─────────┼───────────────┼────────────┼──────┼──────────┼───────────────┘
                                        │
┌───────────────────────────────────────┼──────────────────────────────────┐
│  Application Contract                 │                                  │
│  ┌────────────────────────────────────┴─────────────────────────────┐   │
│  │ versioned Pydantic requests/responses + registered handlers       │   │
│  │ CLI flags and ct api schema adapt the same contract registry      │   │
│  └────────────────────────────────────┬─────────────────────────────┘   │
├───────────────────────────────────────┼──────────────────────────────────┤
│  Analysis Layer          │  Metrics Layer                               │
│  ┌───────────────────────┐│  ┌──────────────────────────────────────┐   │
│  │ projections           ││  │ token usage, reported cost           │   │
│  │ event_scan            ││  │ context stats, token usage           │   │
│  │ item_details          ││  │ tool usage                            │   │
│  │ session_graph_views   ││  │                                      │   │
│  └───────────┬───────────┘│  └──────────────┬───────────────────────┘   │
├──────────────┼────────────┼─────────────────┼────────────────────────────┤
│  Query Layer │            │                 │                            │
│  ┌───────────┴────────────┴─────────────────┴──────────────────────┐    │
│  │ DocumentStore — in-memory UUID-indexed canonical resources      │    │
│  └───────────┬─────────────────────────────────────────────────────┘    │
├──────────────┼───────────────────────────────────────────────────────────┤
│  Discovery   │                                                           │
│  ┌───────────┴──────────────────────────────────────────────────────┐   │
│  │ auto-discover logs → stabilize IDs → assemble session graphs     │   │
│  │ index cache: ~/.coding-trajectory/index.json                     │   │
│  └───────────┬──────────────────────────────────────────────────────┘   │
├──────────────┼───────────────────────────────────────────────────────────┤
│  Ingestion   │                                                           │
│  ┌───────────┴──────────────────────────────────────────────────────┐   │
│  │ Adapter → TranscriptRecord[] → TranscriptProjector → Session     │   │
│  │  ┌──────────┐  ┌─────────────┐  ┌────────┐                       │   │
│  │  │ Codex    │  │ Claude Code │  │ Pi     │                       │   │
│  │  └──────────┘  └─────────────┘  └────────┘                       │   │
│  └──────────────────────────────────────────────────────────────────┘   │
├──────────────────────────────────────────────────────────────────────────┤
│  Vendor Logs (JSONL)                                                     │
│  ~/.codex/sessions  │  ~/.claude/projects  │  ~/.pi/agent/sessions      │
└──────────────────────────────────────────────────────────────────────────┘
```

### Key Architectural Decisions

- **Canonical core, vendor-local adapters**: Vendor-specific parsing never leaks into the core model. Adapters emit a shared transcript IR (`TranscriptRecord`), and a single `TranscriptProjector` state machine constructs the canonical hierarchy.
- **Deterministic UUID5 IDs**: All event, turn, item, and session IDs are UUID5 derived from vendor, source path, index, and content. The same log file always produces the same IDs across runs, enabling stable references and caching.
- **Graph-native multi-agent**: Parent/child sessions, forks, sidechains, and handoffs are first-class `SessionEdge` relationships, not ad-hoc metadata. Connected components are assembled via union-find.
- **No presentation in canonical fields**: UI concerns (sections, roles, workflow labels) live in projections, not the core model.
- **Plugin isolation**: Plugins are separate scripts dispatched from source via a manifest-discovered plugin table (`plugin.toml` per plugin). They do not import core packages; they consume documented CLI outputs or the structured `ct api` service surface.
- **Source-dispatched plugins**: The CLI maps each plugin name to its source directory and entry script. No registration, manifest files, or separate installation step is required.
- **One-way dependencies**: CLI depends on core. Plugins depend on documented
  `ct --output json` contracts or `ct api` request/response contracts. Core
  never depends on plugin packages.
- **Typed service methods**: Each method has a versioned Pydantic request and
  response model plus a named handler. `ct api schema METHOD` returns that
  contract without loading sessions; `ct api` exposes the same methods for
  plugin and automation callers.

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Runtime | Python >= 3.12 | — |
| Data models | Pydantic 2 | Canonical hierarchy + vendor extensions |
| Build system | Hatchling | Per-package wheel builds |
| Package manager | uv workspace | Core, CLI, independent plugins, benchmarks |
| Linting | Ruff | Formatter + linter |
| Datahub web app | React + TanStack | Served by Python HTTP server |

## Project Structure

```
coding-trajectory/
├── packages/
│   ├── core/                               # Canonical models, ingestion, query
│   │   └── src/coding_trajectory/
│   │       ├── __init__.py                 # Public API re-exports
│   │       ├── contracts.py                # Versioned Pydantic service contracts
│   │       ├── discovery.py                # Auto-discover vendor logs, project scoping, ID stabilization
│   │       ├── service.py                  # Method dispatch, serialization, index cache
│   │       ├── query.py                    # DocumentStore: UUID-indexed canonical resource store
│   │       ├── ingestion/
│   │       │   ├── models.py               # Pydantic canonical models (Event, Item, Turn, Session, SessionGraph)
│   │       │   ├── transcript.py           # TranscriptRecord IR + TranscriptProjector state machine
│   │       │   ├── graph.py                # SessionGraph assembly, edge building, union-find components
│   │       │   ├── items.py               # Item append/update helpers
│   │       │   ├── common.py               # Shared utilities (normalize_project_key, prune_nones, etc.)
│   │       │   ├── indexes.py              # Ingestion-time index structures
│   │       │   └── adapters/
│   │       │       ├── base.py             # BaseAdapter ABC + account identity inference
│   │       │       ├── codex.py            # Codex CLI adapter
│   │       │       ├── claude_code.py      # Claude Code adapter
│   │       │       └── pi.py              # Pi adapter
│   │       ├── ingestion/vendor_mechanisms/
│   │       │   ├── relation_edges.py       # Edge type classification
│   │       │   ├── claude_subagent.py      # Claude subagent detection
│   │       │   ├── claude_team.py          # Claude team member/task state
│   │       │   ├── codex_multi_agent.py    # Codex spawn/collaboration modes
│   │       │   └── usage_metrics.py        # Vendor-specific usage extraction
│   │       ├── analysis/
│   │       │   ├── projections.py          # Compatibility facade for analysis projections
│   │       │   ├── session_graph_views.py  # Session graph overview + narrative
│   │       │   ├── event_scan.py           # Filtered event search by type
│   │       │   ├── item_details.py         # Enriched item information
│   │       │   ├── tool_summary.py         # Tool usage aggregation
│   │       │   ├── tool_summary_shell.py   # Shell-specific tool summary
│   │       │   ├── tool_summary_shared.py  # Shared tool summary logic
│   │       │   ├── tool_optimization.py    # Tool-specific output optimization profiles
│   │       │   ├── activity_flow.py        # Temporal activity reconstruction
│   │       │   ├── request_lineage.py      # User request → response tracing
│   │       │   ├── teammate_summary.py     # Multi-agent team analysis
│   │       │   ├── concepts.py             # Shared analysis concepts
│   │       │   └── projection_utils.py     # Shared projection helpers
│   │       └── metrics/
│   │           ├── analysis.py             # Log-derived token, cost, and context builders
│   │           ├── models.py               # Metric Pydantic models (flat + nested)
│   │           └── context_stats/          # Context window category analysis
│   ├── cli/                                # `ct` command-line interface
│   │   └── src/coding_trajectory_cli/
│   │       ├── cli.py                      # CLI entry point (argparse)
│   │       ├── _shared.py                  # Shared CLI formatting helpers
│   │       ├── plugins.py                  # Plugin registry + dispatch
│   │       └── commands/
│   │           ├── project.py              # `ct project list|sessions`
│   │           ├── session.py              # `ct session ...` one-thread views
│   │           ├── graph.py                # `ct session graph ...` orchestration-run views
│   │           └── plugin.py               # `ct plugin list|<name>` (dispatch + index)
│   └── plugins/                            # Built-in executable plugins
│       ├── datahub/                        # `datahub` plugin
│       │   ├── pyproject.toml
│       │   ├── datahub.py                # Datahub CLI entry point
│       │   ├── datahub_web.py            # Python HTTP server for web datahub
│       │   ├── code_time.py              # `code-time` report and forecast surfaces
│       │   ├── context_window.py           # Context composition projection
│       │   ├── token_pricing.py             # models.dev pricing and model metadata
│       │   ├── cleanup.py                  # Project/session cleanup logic
│       │   └── web/dist/                   # Built React frontend
├── benchmarks/                             # Performance benchmarks
│   ├── src/
│   └── results/
├── docs/
│   ├── architecture.md                     # This document
│   ├── prd.md                              # Product requirements
│   ├── cli.md                              # CLI design spec
│   ├── plugin.md                           # Plugin system spec
│   └── roadmap.md                          # Roadmap
├── pyproject.toml                          # Workspace root (uv)
└── AGENTS.md                               # Agent coding rules
```

## Features

### Implemented

| Feature | Description |
|---|---|
| Multi-vendor ingestion | Adapters for Codex CLI, Claude Code, and Pi JSONL logs |
| Canonical normalization | Agent-agnostic Event/Item/Turn/Session/SessionGraph hierarchy |
| Transcript projector | Shared state machine reconstructs Turn → Item from vendor-neutral IR |
| Session graph assembly | Union-find connected components, edge classification (spawned, forked, sidechain, handoff) |
| Deterministic IDs | UUID5-based stable IDs for events, turns, items across runs |
| Auto-discovery | Project-scoped log detection from `~/.codex`, `~/.claude`, `~/.pi` |
| Index cache | Persistent path → session graph mapping at `~/.coding-trajectory/index.json` |
| Document store | In-memory UUID-indexed store with cross-resource navigation |
| Service contracts | Versioned Pydantic requests/responses with registered handlers |
| Token usage | Per-turn, per-session token accounting from normalized session logs, plus log-reported cost when available |
| Context stats | Context window utilization, category breakdown, token usage tracking |
| Tool usage analysis | Tool invocation counts, output sizes, token attribution |
| CLI (`ct`) | Progressive-disclosure command surface with markdown + JSON output |
| Plugin system | Source-dispatched plugins via `plugin.toml` discovery, no core imports |
| Command schema | Cheap `ct api schema METHOD` request/response introspection |
| Datahub plugin | Web visualization plus models.dev pricing and model metadata enrichment |
| Context window view | Context composition bar with event selection and hover preview |
| Datahub cleanup | Project/session cleanup with dry-run, trash, and interactive workflow |

## Service API

The application layer implements versioned Pydantic method contracts consumed
by the CLI. Automation and plugins use `ct api call` or `ct api batch`; dedicated
commands provide a separate compact presentation shape for interactive use.

`ServiceRuntime` is the shared in-process executor behind ordinary CLI commands,
`ct api call`, and `ct api batch`. It owns contract validation, index persistence,
and compatible store reuse for the lifetime of one invocation. Batch remains an
explicit collection of independent calls; shell pipelines and tools such as
`jq` handle external result composition.

### Methods

| Method | Purpose |
|---|---|
| `project.list` | List all discovered projects with vendors and paths |
| `project.sessions` | List orchestration runs, optionally including bulk runtime and usage summaries |
| `session.tree` | Ordinary conversation-fork tree with branch-owned agent counts |
| `session.overview` | Narrative overview: hierarchy, activity keys, turn summaries |
| `session.stats` | Provider usage plus common observed composition buckets and runtime statistics for one thread |
| `session.usage` | Token usage and request-summed cost breakdown by turn for one thread |
| `session.model_usage` | Detailed model and turn-level usage diagnostics for one thread |
| `session.request_usage` | Per-provider-request usage and pricing, with opt-in context and causal diagnostics |
| `graph.overview` | Branch-local orchestration identity, agent sessions, and structural edges, with opt-in narrative turns |
| `graph.stats` | Branch-local aggregate provider usage and runtime, with opt-in per-session composition |
| `graph.usage` | Branch-local aggregate token usage and cost, with opt-in flat graph turns |
| `session.tool_usage` | Per-tool allocation, with opt-in all-item ledger and causal diagnostics |
| `session.items` | Turn-filterable item detail with optional full-content expansion |
| `session.events` | Turn-filterable event query, request-usage selection, and full tool-result dereferencing |

### Store Resolution

1. If a session entry point ID is given and the index cache maps it to source files, perform a **targeted load** (ingest only the relevant files).
2. Otherwise, perform a **full discovery** scan scoped to the current project (or global with `--global-scope`).

### Output Formats

- Report commands default to `--output markdown` for human reading.
- Detail and raw-query commands default to `--output json` for scripting.
- Dedicated-command JSON is minified with a token-efficient CLI schema.
- `ct api schema METHOD` prints the API envelope, canonical result, request,
  and any separately modeled CLI presentation JSON Schemas, then
  exits before discovery, ingestion, or cache mutation.

## Plugin Lifecycle

Plugin routing is CLI-owned. `ct` discovers `plugin.toml` manifests under the plugin root, mapping
each plugin name to its source directory and entry script:

```text
ct plugin list
ct plugin NAME ...
```

No registration, manifest files, or separate installation step is required.
Plugins are dispatched from source via `python <entry>.py <args>` with the
working directory set to the plugin's source directory.

Dependency rules:

```text
coding-trajectory CLI  -> coding-trajectory-core
plugin executable      -> ct subprocess JSON contract
coding-trajectory-core -/-> plugin packages
plugin packages        -/-> core or CLI imports
```

## State Management

### Index Cache

Persisted to `~/.coding-trajectory/index.json`, the cache maps:

| Key | Data |
|---|---|
| `path_to_session_graph` | Source file path → root session ID (avoids re-scanning all logs) |
| `session_to_session_graph` | Session/turn UUID → root session ID (enables targeted loads) |

Stale entries (deleted source files) are pruned on load. The cache is updated after every store resolution.

### Document Store

`DocumentStore` is an in-memory index built on each invocation:

| Index | Type |
|---|---|
| `session_graphs` | `dict[UUID, SessionGraph]` |
| `session_to_root` | `dict[UUID, UUID]` |
| `sessions` | `dict[UUID, Session]` |
| `turns` | `dict[UUID, Turn]` |
| `events` | `dict[UUID, Event]` |
| `items` | `dict[UUID, Item]` |

Supports O(1) lookups by UUID and cross-resource navigation (e.g., session → session graph, turn → session graph).

## Development

### Prerequisites

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) package manager

### Setup

```bash
uv sync                    # Install all workspace dependencies
```

### Commands

| Command | Description |
|---|---|
| `uv run ct project list` | List discovered projects |
| `uv run ct project sessions [PROJECT]` | List sessions for a project |
| `uv run ct session tree SESSION_ID` | Ordinary conversation branches and their owned agent counts |
| `uv run ct session overview SESSION_ID` | One-thread overview |
| `uv run ct session stats SESSION_ID` | One-thread context window statistics |
| `uv run ct session usage SESSION_ID` | One-thread token and log-reported cost breakdown |
| `uv run ct session graph overview SESSION_ID` | One branch's internal agent tree and structural edges |
| `uv run ct session graph stats SESSION_ID` | Branch-local graph context window statistics |
| `uv run ct session graph usage SESSION_ID` | Branch-local graph token and log-reported cost breakdown |
| `uv run ct session items SESSION_ID [ITEM_ID ...]` | Session item detail (JSON) |
| `uv run ct session events --event-id EVENT_ID [--event-id EVENT_ID ...]` | Event detail (JSON) |
| `uv run ct session events SESSION_ID --type TYPE` | Filtered event search |
| `uv run ct plugin list` | List available plugins |
| `uv run ct plugin datahub web` | Start web datahub |
| `uv run ct plugin datahub code-time` | Today's coding time summary |

### Testing

```bash
uv run pytest              # Run test suite
uv run ruff check .        # Lint
uv run ruff format .       # Format
```

## Design Principles

1. **Canonical core, vendor-local adapters** — vendor-specific parsing never leaks into the core model; adapters emit a shared transcript IR.
2. **Deterministic IDs** — UUID5 everywhere enables stable references, caching, and cross-run consistency.
3. **Progressive disclosure** — the API is hierarchical; callers drill into detail by ID rather than receiving everything at once.
4. **No presentation in canonical fields** — UI concerns live in projections, not the core model.
5. **Category-first events** — events are typed and categorized, not raw lifecycle noise.
6. **Plugin isolation** — plugins are separate executables that consume documented CLI outputs, never importing core packages.
7. **Minimal dependencies** — pydantic is the single runtime dependency; no ORM, no database, no framework.
