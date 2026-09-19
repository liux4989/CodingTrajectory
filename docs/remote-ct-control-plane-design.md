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

Internal-workspace facts retain bounded semantic tool descriptions, including
command arguments and tool target paths, for both local reads and publication.
They no longer reduce commands to allowlisted executable names or `command`.
Descriptions remain limited to 280 characters. Common explicitly named
credentials and authorization tokens are redacted; URL userinfo and query
strings are stripped. This is best-effort redaction, not a guarantee that an
arbitrary command is secret-free or safe to share publicly. Upstream summaries
can discard quoting, so multiword credential values may be only partially
redacted. Structural project and file identities retain their existing portable
representation.

Raw tool input/output objects, stdout/stderr, file/patch bodies, complete
transcripts and arbitrary event bodies remain outside the fact contract.
Authentication, workspace isolation, integrity checks and resource bounds are
unchanged. No source data is uploaded merely by changing the local reader.

New preparation uses `ct.graph-preparation.v2`, invalidating the old local and
collector preparation caches by version. Updated readers accept both v1 and v2
artifacts and require the summary version to match its manifest. Old artifacts
remain immutable and cannot recover stripped details; those details require
repreparation from source. Update the Worker and readers before allowing v2
collector publication: old Workers/readers do not support it. Deployment and
republication of existing private data require separate approval.

The exact limits are 512 KiB per row, 16 MiB per graph, 96 MiB per publication,
and 1 MiB/2,048 rows per read page. Normalized SQL staging keeps Worker
materialization graph-bounded independently of the publication aggregate. These
bounds describe `main`, where the larger limits are merged but not
production-qualified or deployed; the 2026-09-16 execution evidence records the
deployed runtime as the earlier clean baseline with tighter bounds (see
[Chronicle history](chronicle-history.md) and the
[upload qualification plan](refactor/upload-qualification-plan.md)).

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
synthetic facts only and refuses non-loopback targets. Its principals fixture
must include the tokens declared in the script, including independent owners
for synthetic workspaces `00000000-0000-0000-0000-000000000001` and
`00000000-0000-0000-0000-000000000002`; the cross-workspace cursor probe uses
both. Deployment, production writes, and workflow state are separate operator
actions.
