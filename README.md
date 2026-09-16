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
