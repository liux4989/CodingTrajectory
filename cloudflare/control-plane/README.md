# Python control plane

The authority runs Python on Cloudflare Workers, with one SQLite Durable Object
per workspace and immutable artifacts in R2. The collector still computes facts
and prepared API responses locally. The Worker validates, publishes, and serves
them; switching the runtime does not move ingestion to Cloudflare.

The stateless entrypoint authenticates and forwards the request stream to its
workspace. `http_handler.py` runs body validation, hashing, artifact I/O, and
response construction inside that Durable Object. This keeps data-dependent
work out of the Workers Free 10 ms CPU budget. Internal workspace operations
reuse the same invocation lock without another RPC hop; upload claim and
completion fences still surround R2 awaits. The Durable Object reauthenticates
the request and rejects a workspace lookup that does not match its own ID.

## Local development

From the repository root:

```sh
uv sync --all-packages --frozen
uv sync --project cloudflare/control-plane --frozen
npm --prefix cloudflare/control-plane ci
npm --prefix cloudflare/control-plane run check
npm --prefix cloudflare/control-plane run dev
```

The Worker has its own Python 3.14 environment and pinned `uv`, `workers-py`, and
SDK. `uv.lock` locks host tooling; `pylock.toml` locks the WebAssembly packages.
`pywrangler sync` installs those packages in ignored `python_modules/`.
Node remains necessary for Wrangler and local integration tooling.

`scripts/bundle-worker-contracts.py` copies the bounded shared Pydantic contract
sources into ignored `src/coding_trajectory/`. Edit models in `packages/core`,
then rerun `check`; do not edit generated copies. Package initializers omit
host-only convenience imports. The bundler rejects new unlisted repository
dependencies. Publication constants live in `control_plane/fact_constants.py`
so importing the artifact contract does not import fact derivation.

Requests are validated directly by Pydantic in strict JSON mode, including model
validators that the old JSON Schema/AJV pipeline could not express. For example,
out-of-range compact manifest references and duplicate objects now fail as
`invalid_contract` at the model boundary. Raw request identity is computed before
defaults and typed normalization. This is an intentional early-development
contract tightening, not an emulation of every former validator behavior.

## Validation and releases

```sh
uv run python scripts/qualify-prepared-api.py
uv run python scripts/qualify-prepared-api.py --shape index-heavy --fixture-output .artifacts/python-worker/index-heavy.json
node scripts/qualify-prepared-api.mjs .artifacts/python-worker/index-heavy.json --publication .artifacts/python-worker/publication.json
node scripts/qualify-legacy-fact-cleanup.mjs
node scripts/benchmark-artifact-publication.mjs .artifacts/python-worker/timing.json --shape representative
uv run python scripts/qualify-deploy-release.py
uv run python scripts/validate-metrics-baselines.py
uv run python scripts/build-python-worker.py --outdir .artifacts/python-worker-build
```

The publication benchmark now uses the current prepared-artifact fixture. Its
`--shape` option replaces the retired v1 graph/orphan-count arguments; results
are not directly comparable to that historical corpus. Local timing and cursor
counters do not establish Cloudflare production latency or billing.

The build destination must be new. The build copies the Python sources, shared
contracts, installed WebAssembly dependencies, lockfiles, and configuration,
then asks Wrangler to dry-run that standalone tree. It does not deploy.

`scripts/deploy-release.py` retains the pinned-source preparation, explicit
staging activation, and no-retry reconciliation workflow. Release plan version 2
seals the complete Python module inventory, not a JavaScript entrypoint. Deploy
uses that copied configuration directly with the locked Wrangler executable;
it does not resolve packages again. Version 1 release plans must be prepared anew.

The Worker name, `Workspace` Durable Object class, SQLite migration, and R2
bindings remain stable. Code rollback does not roll back SQL or R2 writes.
No deployment or data reset is implied by a local build or passing qualification.
