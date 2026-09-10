# Cloudflare control plane

Local vendor logs remain the source of evidence. The optional remote authority
stores bounded Chronicle graphs, portable projects, collector checkpoints,
living observations, and estimator jobs. Datahub's published seven-day snapshot
is built directly from local sources and does not depend on this authority.

## Storage and authorization

`cloudflare/control-plane` contains the native TypeScript Worker. Each workspace
has one SQLite Durable Object selected by its authenticated workspace identity.
SQLite transactions commit source fences, publication manifests, revisions,
retry receipts, leases, and job transitions together. Workspace sequence numbers
pin historical reads; no read merges local evidence with remote state.

The `ARTIFACTS` R2 bucket contains immutable gzip-compressed canonical graphs,
keyed by workspace and SHA-256. Uploads finish before SQLite references are
committed. Failed publication can leave an unreferenced object, but cannot make
it visible through the API. Readers select descriptors at a single sequence
before fetching R2 objects, then verify the digest, decompressed byte count,
and canonical Pydantic graph on the host. Objects are private; there are no
public R2 URLs or client-side bucket credentials.

The Worker secret `CT_PRINCIPALS` maps SHA-256 token digests to objects containing
`workspace_id`, `agent_id`, and `roles`. Generate high-entropy URL-safe bearer
tokens. Available roles are `read`, `collect`, `estimate`, `estimate_worker`, and
`owner`. Give collectors only the roles their workflow requires. A token cannot
select another workspace or claim another collector agent. Rotate by replacing
the secret registry and removing the old digest; account credentials never enter
the Python runtime or collector profile.

## Contracts

The Worker exposes one authenticated `POST /v1/core` endpoint using the
`ct.core.v1` envelope. Its typed `method` and `params` select the collector,
historical, living, or estimation operation used by the Python repositories.
Optional parameters may be explicit `null`; successful responses retain
declared nullable fields instead of omitting them.
Requests use a JSON `request` envelope. Collector calls additionally carry an
idempotency key. Reusing a key with changed content fails; an exact retry returns
the stored receipt. Ingress schemas are generated from canonical Pydantic models
and compiled ahead of time for the Workers runtime.

Source epochs fence stale collectors. Publication requires accepted, contiguous
source checkpoints and the complete source vector of overlapping graphs. A
publication manifest advances atomically, including graph replacement and
omission. Canonical payloads never contain raw logs or complete evidence bodies.

Living heartbeats and canonical changes share a monotonically increasing
instance sequence. Retrying a heartbeat preserves its original expiry. Expired
or missing leases mean unknown state; they do not imply terminal sessions.
Paginated reads fix both workspace sequence and evaluation time, and bind
cursors to workspace, method, and scope. Leases themselves are versioned.

Estimator planning and calibration reuse Python's existing pure functions.
Scoped workers claim jobs with an expiring, attempt-fenced lease, execute the
provider locally, and atomically commit the forecast. Completion is retry-safe;
expired attempts cannot complete. Two exhausted attempts terminate the job.
Backfill parents limit concurrent child leases and expose durable counts.
Provider error bodies are not retained by the remote authority.

## Development and deployment

```sh
uv sync --all-packages
cd cloudflare/control-plane
npm ci
npm run schemas
npm run types
npm run check
npx wrangler r2 bucket create coding-trajectory-artifacts
npx wrangler secret put CT_PRINCIPALS
npm run deploy
```

Provision the registry through secret stdin; never commit tokens or `.dev.vars`.
For local qualification, run the qualification script with `--prepare-local`
to create synthetic principals in `.dev.vars`, then start
`npx wrangler dev --local --port 8794`, and run
`PYTHONPATH=packages/core/src uv run python scripts/qualify-cloudflare-control-plane.py`
from the repository root. It exercises the actual Worker, SQLite, and R2 runtime.
The fixture tokens in that script are for the loopback harness only.

Configure `CT_CLOUDFLARE_URL`, `CT_ACCESS_TOKEN`, and `CT_REMOTE_WORKSPACE_ID`, or
use `ct collector credentials configure` to store a scoped token in macOS
Keychain or name an injected token environment variable. Use a fresh collector
state path when moving to a new authority; local source logs are republished
without copying the old remote database. Read validation and a confirmed live
publication are separate from deployment success.

The retired database migrations and platform-specific release scaffolding have
been removed from the repository. Existing external databases are not deleted
by code cleanup. They are not consulted by the Cloudflare clients or snapshot.
