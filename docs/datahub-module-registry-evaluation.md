# Cloudflare module registry evaluation — 2026-09-10

> Historical evaluation of the frozen snapshot Worker at the revisions below.
> Frozen export support and its benchmark/qualification scripts have since been
> removed. Reproduction requires the historical checkout; these results do not
> qualify the current live Datahub Worker.

**Recommendation: defer.** The current snapshot Worker has no demonstrated module
compatibility problem and the flag alone provides no meaningful local request
latency improvement. Leave its deployment configuration unchanged. Revisit when
a real dependency needs the new resolution semantics, or substantial code is
loaded only by uncommon routes. No separate-module packaging experiment is
justified by this initial comparison.

## Scope and compatibility findings

The comparison uses committed `d9e6156` in an isolated worktree. The original
checkout had concurrent changes, including extracting Access helpers from
`index.ts` into `access.ts`; those changes were not included or modified.
This evaluates the committed direct snapshot path, not the old Python facade.

- [Cloudflare's September 9 announcement](https://blog.cloudflare.com/workers-module-registry-nodejs/)
  makes the flag explicitly opt-in, with no automatic enable date. It improves
  URL resolution, import metadata, CommonJS/ESM interoperability, and deferred
  compilation. Changing the flag does not itself split a Wrangler bundle.
- The current [workerd compatibility schema](https://github.com/cloudflare/workerd/blob/main/src/workerd/io/compatibility-date.capnp)
  declares `new_module_registry` / `legacy_module_registry` without an
  experimental annotation or enable date. The public compatibility-flags page
  retrieved during this evaluation did not contain the flag; the announcement
  and current source supply the details. Older indexed source snippets still
  show an experimental annotation and should not be used to determine current
  availability.
- Installed Wrangler **4.129.1**, Miniflare **5.20260907.0-alpha**, workerd
  **1.20260907.1**, esbuild **0.27.7**, Node **22.22.0**, and jose **6.2.12**
  successfully built and ran both configurations. No tool upgrade was needed;
  this establishes support in these versions, not the minimum supported version.
  Wrangler's installed schema supports `rules`, `find_additional_modules`, and
  `no_bundle`, but none was changed.
- The Worker has 14 contributing source files flattened into **one ESM output**:
  12 `jose/dist/webapi` files and two local Worker files. No runtime import,
  dynamic import, `require()`, `import.meta`, Node built-in import, JSON module,
  or Wasm module remains in the emitted JavaScript. No relevant incompatibility
  was found. The legacy gateway forwarding code is removed by tree shaking.
- JWT verification uses Web Crypto and fetch. It is required on every authorized
  route, so deferring its loading would move work onto the first authorized
  request. The JSON snapshot is read through `ASSETS.fetch`, not imported as code.
  React, charting, and other frontend dependencies are served browser assets;
  their loading is outside the Worker module registry.
- [workerd's feature selector](https://github.com/cloudflare/workerd/blob/main/src/workerd/io/features.h)
  explicitly keeps Python Workers on the legacy registry even when the flag is
  configured. These Node/ESM changes are not a remedy for Python Worker failures.

## Measurements

Apple M4, macOS arm64; serial execution. Both variants retain compatibility date
`2026-09-10` and `nodejs_compat`. The sole flag difference is appending
`new_module_registry`. Application source, installed dependencies, and a copied
snapshot asset directory stayed fixed. The harness checks source/lock and asset
digests before and after. All 120 manifest file digests passed; the full asset
inventory contains 144 files. Snapshot contents are not committed with results.

One complete warmup pair is discarded; 12 fresh runtimes per variant alternate
AB/BA order. Each runtime receives an authenticated snapshot request first, 14
other parity cases, then 50 sequential authenticated snapshot requests. The
unchanged verifier checks a synthetic RS256 JWT against a local JWKS response.
No real Access credentials, policy bypass in application code, or outbound
Cloudflare requests are involved in the request benchmark.

Final run, milliseconds (median / nearest-rank p95):

| Measurement | Existing | New registry |
| --- | ---: | ---: |
| Local Miniflare construction → ready, n=12 | 57.33 / 125.69 | 65.68 / 83.15 |
| First authenticated request + body read, n=12 | 5.79 / 12.46 | 5.99 / 7.94 |
| Steady request + body read, n=600 | 0.890 / 3.283 | 0.874 / 1.909 |
| Median of each runtime's steady median, n=12 | 0.875 | 0.859 |
| Wrangler sampled startup active time, n=12 | 0.0 / 2.6 | 1.3 / 1.3 |
| Emitted JS bytes, including source-map comment | 46,046 | 46,046 |
| Gzip bytes | 12,637 | 12,637 |
| Emitted application modules / remaining imports | 1 / 0 | 1 / 0 |

Both emitted scripts have SHA-256
`2c8622a7f40978d546ddba4310336c4cf379dba63d2d7dbc3e9ef5935c338fa0`.
Output comments and hashes can vary with checkout paths; equality within a run
is asserted. Aggregate and per-runtime measurements are in
[datahub-module-registry-measurements.json](datahub-module-registry-measurements.json).

The small steady median difference is not evidence of a useful speedup. An
earlier full run had first-request medians of 6.29 ms for both settings and steady
medians of 0.925 / 0.962 ms, reversing the steady ordering. Tail values are noisy;
with 12 runtimes, the reported first-request p95 is simply the maximum.

**Startup caveat:** Wrangler 4.129.1 profiles a wrapper that dynamically imports
the bundle after readiness. Legacy eager compilation may happen before sampling,
whereas new-registry compilation may occur inside the sampled window. Its sparse,
rounded active samples are not a comparable total isolate-startup metric; zero
does not mean zero startup cost. Readiness includes Miniflare, process, and local
asset setup. Request times include the Node driver, local bridge, crypto, and
asset emulation. None is live Cloudflare CPU time or an edge cold-start measure.
Cross-isolate compile-cache reuse described in the
[runtime reference](https://github.com/cloudflare/workerd/blob/main/docs/reference/detail/new-module-registry.md)
is not measured by fresh local processes.

## Behavioral validation and limits

All 15 benchmark cases matched status, body digest, and headers, excluding Date,
ETag, and Last-Modified. Cases cover successful snapshot/list/projects/changes and
HTML responses; private snapshot denial; invalid/duplicate queries, identifiers,
and methods; disabled content hydration; unknown routes; and missing/malformed
JWTs. Every response retained `no-store`, and each runtime fetched JWKS once.

The existing `qualify-datahub-snapshot.py` passed **188 calls per variant**,
including all exported graph/tree responses, pagination, filters, metadata items,
and signed-out entrypoint checks. That existing qualification harness invokes
the exported API directly; the benchmark separately exercises the signed entrypoint.
Worker typechecking passed. The full metrics baseline workflow passed all four
cases (107 assertions, 49 invariants); its initial missing-Pydantic environment
was resolved with `uv sync --all-packages --frozen`, without source/lock changes.

**Live evidence: none collected.** No version was uploaded, no production or
candidate deployment was made, and no Access policy was changed. Dry-run success
and local signed-JWT parity do not prove edge startup, Access service integration,
global first-request latency, or production traffic parity. Snapshot payload and
frontend asset changes in concurrent work are outside this baseline.

## Reproduce

Use this evaluation branch based on `d9e6156` in an isolated worktree. Install the
web dependencies from its frozen `bun.lock` (`bun install --frozen-lockfile` in
`packages/plugins/datahub/web`), or reuse a verified matching installed dependency
tree as this run did. Copy an existing sanitized `web/dist` into the worktree;
do not regenerate or publish it between variants. The original run reused the
installed dependency directory read-only and copied the assets once.

```sh
node scripts/benchmark-module-registry.mjs
# Defaults: 12 pairs; ignored output at .artifacts/module-registry.
# Optional: ROUNDS=24 node scripts/benchmark-module-registry.mjs /tmp/ct-registry
npm --prefix packages/plugins/datahub/web run check:worker
uv sync --all-packages --frozen
uv run python scripts/validate-metrics-baselines.py
scripts/check-metrics-quality-gate.sh
```

The benchmark generates two temporary configs and runs these for each variant:

```sh
WEB=packages/plugins/datahub/web
OUT=.artifacts/module-registry
VARIANT=baseline # repeat with new
"$WEB/node_modules/.bin/wrangler" deploy --dry-run -c "$OUT/$VARIANT.json" \
  --outdir "$OUT/$VARIANT" --metafile "$OUT/$VARIANT.meta.json"
"$WEB/node_modules/.bin/wrangler" deploy --dry-run -c "$OUT/$VARIANT.json" \
  --outfile "$OUT/$VARIANT.bundle"
"$WEB/node_modules/.bin/wrangler" check startup -c "$OUT/$VARIANT.json" \
  --workerBundle "$OUT/$VARIANT.bundle" --outfile "$OUT/$VARIANT.cpuprofile"
```

Passing a prebuilt multipart bundle avoids Wrangler's nested build losing the
explicit config in this layout. CPU profiles and full timing arrays stay local.
Repeat existing qualification for both flags without modifying its tracked file:

```sh
uv run python - <<'PY'
from pathlib import Path
path = Path('scripts/qualify-datahub-snapshot.py').resolve()
source = path.read_text()
for variant in ('baseline', 'new'):
    print(variant, flush=True)
    code = source if variant == 'baseline' else source.replace(
        '["nodejs_compat"]', '["nodejs_compat", "new_module_registry"]')
    exec(compile(code, str(path), 'exec'),
         {'__file__': str(path), '__name__': '__main__'})
PY
```
