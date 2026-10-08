# CodingTrajectory

CodingTrajectory converts coding-agent logs into a common session model.
Use the CLI to inspect sessions and measurements. Use Loop to read the same
evidence in a browser.

Supported log sources are Codex CLI, Claude Code, Pi, and the Amp capture plugin.
Provider coverage differs. Missing evidence remains unavailable; it does not
become zero.

## Start locally

Run these commands from the repository root. Python 3.12 or later and `uv` are
required. Loop also requires Bun.

```sh
uv sync --all-packages --locked
uv run ct --source local project list
uv run ct --source local project sessions --project-id PROJECT_ID
```

Replace `PROJECT_ID` with an ID from `project list`. For browser access:

```sh
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

Loop reads local evidence. It does not publish data or use remote fallback.
Remote publication and deployment are separate, explicitly authorized operations.

## Core documentation

| Read | Use it to |
| --- | --- |
| [CLI](docs/cli.md) | Find sessions, read evidence, and configure connections |
| [Loop](docs/loop-design.md) | Investigate sessions and run token-budget watches |
| [Architecture](docs/architecture.md) | Understand data flow and ownership |
| [Operations](docs/operations.md) | Capture Amp logs, publish data, and deploy releases safely |
| [Core protocol](docs/core-protocol.md) | Review and version public contracts |
| [Token glossary](docs/token-usage-glossary.md) | Interpret token counts, costs, and processing or output speed |
| [Metrics validation](docs/metrics-validation-quality-gate.md) | Check changes against audited source evidence |

Worker-specific commands are in the [Worker guide](cloudflare/control-plane/README.md).
Benchmark rules are in the [benchmark guide](benchmarks/README.md).
[RELEASE.md](RELEASE.md) contains the release marker, not a deployment receipt.

## Checks

```sh
uv run ruff check .
uv run python scripts/check-core-protocol.py
scripts/check-metrics-quality-gate.sh
uv run python scripts/validate-metrics-baselines.py
bun run --cwd packages/plugins/loop/web check
uv run python scripts/check-loop.py
```

Run checks relevant to the change. Follow [AGENTS.md](AGENTS.md) before committing.
Local checks do not prove that a release is deployed or that production data is current.

## Documentation rules

Use a practical ASD-STE100-inspired style, not a claim of full compliance:

- Use short sentences. Aim for 20 words in instructions and 25 in descriptions.
- Use active voice, simple verb forms, and one term for each concept.
- Put one action in each numbered procedure step.
- State the condition before the action. Put warnings before write operations.
- Keep command names, field names, and necessary technical terms exact.
- Describe current behavior. Label limits and unavailable evidence explicitly.

Keep one guide for each core topic. Keep legal notices, tool instructions, and
audited validation evidence beside their owners. Do not add dated reports or
superseded plans to `docs/`; Git history retains those records.

To read a removed document, find its last content commit, then use `git show`:

```sh
git log --all --diff-filter=AM -- docs/PATH.md
git show COMMIT:docs/PATH.md
```
