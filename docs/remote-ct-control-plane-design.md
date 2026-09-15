# Cloudflare control plane

The optional remote authority stores bounded historical facts, portable project
and source metadata, collector checkpoints, and living observations. Provider
logs remain on their originating host.

## Storage and authorization

Each authenticated workspace maps to one SQLite Durable Object. Versioned
`fact_rows` retain stable graph/kind/fact identity, parent/order metadata, a row
hash, bounded typed payload, and validity sequences. Graph publications,
checkpoints, receipts, projects, sources, and living leases are committed in the
same workspace authority. There is no R2 historical authority, compressed graph
payload, chunk/pack/manifest reconstruction, duplicate catalog/projection family,
or remote reconstruction cache.

`CT_PRINCIPALS` maps bearer-token SHA-256 digests to workspace/agent/role claims.
`CT_CURSOR_KEY` is a separate server-held HMAC key of at least 32 bytes for fact
read cursors. Rotating it invalidates outstanding cursors, whose callers must
restart from a pinned snapshot. Neither secret is returned or logged.
The Worker validates the authenticated workspace and collector identity; callers
cannot select another workspace or agent. Tokens and provider credentials never
enter fact rows.

## Contracts

`POST /v1/core` accepts the generated `ct.core.v1` method envelope. Collectors
register projects/sources, publish metadata-only checkpoints, stage typed rows,
and atomically publish graph manifests. The authority verifies canonical row
hashes and set digests, payload/fact identity, parent and cross-reference
ownership, cardinality, source fences, and size limits before commit.

Historical reads select graph sets by stable graph/session identity or bounded
project filters, pin one workspace sequence, and return deterministic SQL pages.
HMAC-authenticated cursors bind their continuation tuple, sequence, and normalized
selector/kinds scope. Cloudflare
does not calculate summaries or metrics; Python reconstructs `PublishedFactSet`
values and runs the same historical handlers used locally.

Published command evidence contains only an allowlisted executable signature,
never arguments. Standard APIs contain no raw transcript, prompt, reasoning,
tool input/output, arbitrary event body, secret, or host-absolute path.

The exact limits are 512 KiB per row, 8 MiB per graph, 16 MiB per publication,
and 1 MiB/2,048 rows per read page. The aggregate caps prevent a valid collector
from forcing isolate-scale buffering despite each graph being individually valid.

Living methods remain separately versioned. Forecasting and calibration remain
local/Core authorities; no estimator route or job store exists in this Worker.

## Local qualification

Create a loopback-only principals JSON outside the repository, then run:

```sh
cd cloudflare/control-plane
npm ci
npm run check
npx wrangler dev --local --port 8794 --persist-to /tmp/ct-facts-qualification \
  --var "CT_PRINCIPALS:$(cat /tmp/ct-principals.json)" \
  --var "CT_CURSOR_KEY:local-qualification-cursor-key-00000001"
```

From the repository root, run
`uv run python scripts/qualify-cloudflare-control-plane.py`. The qualifier uses
synthetic facts only and refuses non-loopback targets. Deployment, production
writes, and workflow state are separate operator actions.
