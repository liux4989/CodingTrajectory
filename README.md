# coding-trajectory

Unified canonical models and CLI tooling for coding-agent session graphs.

## CodingTrajectory Loop

Loop is the local-first Analytics product: explore local sessions, open an
investigation, and resolve stable item/event references back to canonical evidence.
It saves reading positions, never copied transcripts. The deterministic Monitor
foundation (turn-token-budget watches, dry-run, and finding triage) is included;
broader Monitor automation, Improve, and hosted delivery are deferred. See
[Loop design and local usage](docs/loop-design.md).

```bash
uv sync --all-packages --locked
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

## Layering

- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources. They preserve agent-agnostic facts and stable references reconstructed from vendor logs.
- `SessionGraph` preserves unified session lineage internally. The CLI exposes ordinary human forks through `ct session tree` and each branch's orchestration run through `ct session graph`; forked conversations are not aggregated as spawned agents.
- Presentation-oriented interpretations such as replay sections, UI workflows, and enrichment-specific labels do not belong in the core layer.

## Project identity and prepared reads

`project.list` v4 returns an `items` map keyed by opaque project ID. Each value
contains `project_id`, `display_name`, `path`, and `vendors`. Pass the returned ID
to `project.sessions` v4 as `project_id` (CLI: `ct project sessions --project-id ID`).
Names remain a convenience selector, not identity; ambiguous names must use an ID.
Local IDs identify canonical host locations, so distinct directories with the
same name remain distinct. Moving a directory changes its local ID. Remote IDs
come from the workspace registry and survive display-name changes. IDs are
authority-scoped: do not substitute a local ID for a remote ID. Temporary local
locations are listed individually rather than combined under a synthetic project.

Local reads and the collector share graph preparation. Canonical graph content
and the preparation version key a private disposable SQLite cache at
`~/.coding-trajectory/prepared-graphs.sqlite` (128 MiB of cached payloads).
Unchanged graphs reuse projected facts and session cards across processes;
source discovery/parsing and content hashing still run to detect unpublished
changes. The first read of changed data may prepare it locally. Publication
uses the same preparation and applies transport bounds without truncating local
reads. List filters are shared; remote project selection uses manifest ownership
and reads summaries only for that project. Detailed remote reads remain lazy.

The existing zero-item visibility rule is unchanged. Project IDs are attached
at read time, so this refactor does not require resetting remote data or changing
existing immutable facts/summaries. Deploying the new client/contract and Worker
SQL project-ID selector remains a separate rollout; no publication is automatic.

## Docs

- [Documentation index](docs/README.md)
- [Product requirements](docs/prd.md) and [architecture](docs/architecture.md)
- [Chronicle operational history](docs/chronicle-history.md)
- [CLI usage](docs/cli.md)
- [Collector and deployment handoff](docs/local-collector-handoff.md)
- [Benchmark and artifact policy](benchmarks/README.md)

## Checks

- `uv run ruff check .` for repo-wide Python static analysis
- `bun run --cwd packages/plugins/loop/web check` for generated Core consumer types and TypeScript
- `uv run python scripts/check-loop.py` for offline local HTTP integration
- `uv run python scripts/check-core-protocol.py` for the frozen Core boundary
