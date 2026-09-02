# coding-trajectory

Unified canonical models and CLI tooling for coding-agent session graphs.

## Layering

- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources. They preserve agent-agnostic facts and stable references reconstructed from vendor logs.
- `SessionGraph` preserves unified session lineage internally. The CLI exposes ordinary human forks through `ct session tree` and each branch's orchestration run through `ct session graph`; forked conversations are not aggregated as spawned agents.
- Presentation-oriented interpretations such as replay sections, UI workflows, and enrichment-specific labels do not belong in the core layer.

## Docs

- CLI usage: [`docs/cli.md`](docs/cli.md)
- CLI agent notebook: [`docs/cli-agent-notebook.ipynb`](docs/cli-agent-notebook.ipynb)
- Amp local collector: [`docs/amp-collector.md`](docs/amp-collector.md)
- Remote control plane: [`docs/remote-ct-control-plane-design.md`](docs/remote-ct-control-plane-design.md)
- Local collector handoff: [`docs/local-collector-handoff.md`](docs/local-collector-handoff.md)
- PRD & Architecture: [`docs/prd.md`](docs/prd.md)

## Checks

- `uv run ruff check .` for repo-wide Python static analysis
- `scripts/check-datahub-static.sh` for datahub backend undefined-name and import-time checks
