# Python Worker guide

The Worker authenticates workspace requests and serves immutable artifacts.
The collector owns ingestion and historical computation. See the
[architecture](../../docs/architecture.md) for the shared data flow.

The entrypoint forwards request streams to the workspace Durable Object.
`http_handler.py` validates bodies, hashes content, performs artifact I/O, and
constructs responses there. The Durable Object reauthenticates requests and
checks workspace identity. Upload claims and completion fences protect R2 operations.

## Local development

Run from the repository root:

```sh
uv sync --all-packages --frozen
uv sync --project cloudflare/control-plane --frozen
npm --prefix cloudflare/control-plane ci
npm --prefix cloudflare/control-plane run check
npm --prefix cloudflare/control-plane run dev
```

The Worker uses Python 3.14 and pinned `uv`, `workers-py`, and SDK versions.
Its `uv.lock` locks host tooling; `pylock.toml` locks WebAssembly packages.
`pywrangler sync` installs packages in ignored `python_modules/`.
Node is required for Wrangler and local integration tooling.

`scripts/bundle-worker-contracts.py` copies shared Pydantic models into ignored
`src/coding_trajectory/`. Edit the source models in `packages/core`, then rerun
`check`. Do not edit generated copies. The bundler rejects unlisted dependencies.
Keep artifact constants independent of host-only fact derivation.

Requests use strict Pydantic JSON validation, including model validators.
Invalid compact references and duplicate objects fail as `invalid_contract`.
Request identity is computed before defaults and typed normalization.

## Local qualification

```sh
uv run python scripts/qualify-prepared-api.py
uv run python scripts/qualify-prepared-api.py --shape index-heavy \
  --fixture-output .artifacts/python-worker/index-heavy.json
node scripts/qualify-prepared-api.mjs .artifacts/python-worker/index-heavy.json \
  --publication .artifacts/python-worker/publication.json
uv run python scripts/qualify-deploy-release.py
uv run python scripts/validate-metrics-baselines.py
uv run python scripts/build-python-worker.py --outdir .artifacts/python-worker-build
```

The build destination must be new. The build copies sources, contracts,
WebAssembly dependencies, locks, and configuration. Wrangler dry-runs the standalone
tree; it does not deploy.

For local publication timing:

```sh
node scripts/benchmark-artifact-publication.mjs \
  .artifacts/python-worker/timing.json --shape representative
```

The benchmark uses prepared-artifact fixtures. Local timings and cursor counters
do not prove production latency, billing, or capacity. Historical corpora used
different shapes and are not directly comparable.

## Releases

Follow the [operations guide](../../docs/operations.md#prepare-and-deploy-a-release).
Prepare once; activate explicitly for `staging` or `production`.
Plan versions 2 and 3 seal the Python module inventory and copied configuration.
Version 1 plans require fresh preparation. Deploy uses the sealed dependencies;
it does not resolve packages again.

**Warning:** code rollback does not roll back SQLite or R2 writes.
Builds and qualification do not authorize deployment, data resets, or publication.
Routine releases include compatibility preflight and smoke reads, not data replacement.
