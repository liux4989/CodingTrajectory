# coding-trajectory

Unified canonical models and CLI tooling for coding-agent session graphs.

## Layering

- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources. They preserve agent-agnostic facts and stable references reconstructed from vendor logs.
- `SessionGraph` is the orchestration aggregate over canonical sessions. Its identity is the root session id, and it exposes observed membership, orchestration capabilities, edges, and summary metadata. The CLI hierarchy is `ct graph` → `ct session`; there is no separate workflow tree.
- Presentation-oriented interpretations such as replay sections, UI workflows, and enrichment-specific labels do not belong in the core layer.

## Docs

- CLI usage: [`docs/cli.md`](docs/cli.md)
- CLI agent notebook: [`docs/cli-agent-notebook.ipynb`](docs/cli-agent-notebook.ipynb)
- PRD & Architecture: [`docs/prd.md`](docs/prd.md)
- Evaluation mechanism: [`docs/evaluation-design.md`](docs/evaluation-design.md)

## Checks

- `uv run ruff check .` for repo-wide Python static analysis
- `scripts/check-dashboard-static.sh` for dashboard backend undefined-name and import-time checks
