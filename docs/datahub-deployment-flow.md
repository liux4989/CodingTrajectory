# Datahub deployment flow

Status: proposed release design; implementation and live qualification pending.
Date: 2026-09-10.

This runbook redesigns delivery of the private non-production Datahub described
in [private hosting](datahub-cloudflare-private-hosting.md). It does not certify
the current preview as healthy. The dedicated-reader cutover is unfinished.

## Architecture decision

Keep the existing request path:

```text
Browser -> Cloudflare Access -> gateway + static assets
                                  -> private service binding
                                      -> Python CT facade -> Supabase RPC
```

Cloudflare admits the owner. The facade uses a dedicated Supabase Auth reader
whose database permissions restrict access to the intended workspace. Python
retains CT semantics and Pydantic contracts. Supabase retains canonical remote
data. The browser receives no database credentials. The collector remains a
separate publisher with its existing seven-day-plus-graph-closure scope.

This is an owner-only application decision, not a general multi-user identity
mapping. Adding more Access users would give them the same reader's scope and
requires a separate authorization design.

Use service bindings for Worker-to-Worker calls and encrypted Worker secrets
for credentials, following [Cloudflare best practices](https://developers.cloudflare.com/workers/best-practices/workers-best-practices/).
Keep PostgREST HTTPS for the current RPC transport; introducing direct Postgres
connections or another compute service is not needed for this release design.

## Three independent operations

| Operation | Trigger | Required result |
| --- | --- | --- |
| Environment provisioning | New environment or explicit identity/configuration change | Verified account, project, Access policy, private bindings, reader permissions, secret names |
| Database migration | Committed schema changes | Local replay, remote dry run, reviewed target, applied migration history and authorization checks |
| Application release | Committed application changes | Reproducible bundles, runtime qualification, candidate data-route checks, recorded promotion and rollback versions |

Ordinary application releases neither reset databases nor recreate Auth users.
Retain separate local, non-production, and future production configuration.
Use an isolated Supabase branch/project for schema candidates; application-only
candidates may read the approved preview database with the verified reader.
Supabase documents isolated environments and migration delivery through CI in
[Managing Environments](https://supabase.com/docs/guides/deployment/managing-environments).

Infrastructure setup must become a versioned, idempotent provisioning operation
with a dry-run diff. Dashboard access is a bootstrap/recovery path. Runtime
credentials must not have infrastructure-administration authority. Explicit
environment manifests identify Worker names, Access audience, service targets,
Supabase project, and secret references; no target is inferred solely from a
developer's last CLI login or linked-project cache.

## Release stages

Stop at the first failed stage. Permit only one mutating release per environment;
keep build/check fan-out at two jobs. A restarted release reconciles actual
versions before resuming instead of blindly repeating deployment.

### 1. Freeze and build

Build from a clean checkout of a recorded commit with frozen Python and npm
lockfiles and recorded uv, pywrangler, Wrangler, and Supabase CLI versions.
Review and commit the existing cutover edits before making them a release input.

Build Python dependencies into a fresh staging directory using pywrangler's
supported packaging workflow. The current `prepare-hosted-worker.sh` combines
dependency synchronization and installation into `python_modules`; replace or
harden that path so entrypoint modules cannot resolve a stale second package
copy. Record module origins and hashes from the staged artifact, and exercise
those imports inside workerd. Do not repair an artifact after validating it.

Cloudflare's [Python package documentation](https://developers.cloudflare.com/workers/languages/python/packages/)
describes pywrangler's package bundling. Keep one explicit import layout and
exclude local-only dependencies where contracts allow it.

Run existing API generation consistency, Worker type checking, hosted web build,
Python lint, and relevant integration qualifications. For metric-sensitive
changes, run both repository metric gates without regenerating expectations
from observed output. Do not add unit tests for this flow.

### 2. Qualify Python runtime

Capture startup measurements and runtime exceptions for the exact candidate.
Cloudflare currently documents a one-second global startup limit and deployment
rejection code 10021. Error 1101 alone does not establish startup exhaustion.
Use the [limits and profiling guidance](https://developers.cloudflare.com/workers/platform/limits/)
to distinguish deployment validation, startup, imports, and request failures.

Python top-level imports are executed during deployment and included in a memory
snapshot. Moving all imports into handlers can transfer work to requests rather
than eliminate it. Profile before choosing eager or deferred imports, as
explained in [How Python Workers Work](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/).

Adopt 700 ms as an initial project startup budget, not a Cloudflare guarantee.
Require fresh-version first requests and repeated requests to succeed with
representative bounded data; record first-request latency separately from
startup and steady-state latency. Merely waiting between requests does not prove
a cold isolate. Missing runtime evidence blocks promotion even when dry-runs pass.

### 3. Qualify database and reader

For schema changes, replay committed migrations in an isolated local database
with synthetic fixtures, inspect the remote migration dry run, then apply only
pending migrations to the explicitly authorized non-production target. Shared
resets remain a separate destructive operation. Use forward corrective migrations
for deployed history. See [Supabase database migrations](https://supabase.com/docs/guides/deployment/database-migrations).

Check the reader through the actual PostgREST role and JWT: intended workspace
reads succeed, another workspace is denied or invisible, and publish/mutation
RPCs and direct writes are denied. Exercise destructive denial cases only with
synthetic data in isolation. Verify live grants and policies read-only afterward.
One membership row and zero collector capabilities are necessary evidence, but
do not alone prove the entire principal is read-only.

Audit exposed RPC execution grants and every `SECURITY DEFINER` function's
explicit authorization and fixed search path. Table RLS alone cannot establish
their safety. Supabase recommends invoker functions by default and explicit
function privileges in its [function guidance](https://supabase.com/docs/guides/database/functions)
and documents [RLS boundaries](https://supabase.com/docs/guides/database/postgres/row-level-security).

Verify secret presence without printing values. Provisioning and rotation occur
separately from code builds. Account for secret operations that create deployments;
do not unintentionally activate an unqualified version. See
[Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/).

### 4. Deploy and validate a candidate pair

Use a separate private candidate facade and an Access-protected candidate gateway
bound to it. Verify Access coverage before publishing any candidate hostname;
disable implicit preview URLs and public facade routes. The currently serving
gateway must retain its previous backend while the candidate is evaluated.

Upload/version operations and activation are separate release states. Confirm
which version operations the pinned Python toolchain supports; if an operation
is unsupported, deploy to the isolated candidate Worker instead of mutating the
serving facade. Cloudflare distinguishes [versions from deployments](https://developers.cloudflare.com/workers/versions-and-deployments/).

Require signed-out denial, forged assertion denial, authorized owner access,
and all seven allowed API response schemas through the actual candidate gateway.
Check prohibited routes, invalid bounds, API JSON rather than SPA fallback,
deep navigation, snapshot consistency, and credential-free browser assets.
Validate Sessions and Graph with representative data. Auth denial or a rendered
HTML shell is insufficient to establish API health.

Use the owner's authorized browser for the initial private preview validation.
Do not introduce an Access bypass or public health route for CI convenience.
If automated authenticated probes later require a machine identity, define its
narrow policy explicitly; unattended promotion remains unavailable until then.

### 5. Promote, observe, and roll back

Promote the validated gateway/assets with its binding to the qualified private
facade. Retain the previous facade and gateway version as a compatible pair.
Two Worker deployments are not an atomic transaction; a backend slot must not be
overwritten while an active or rollback gateway still refers to it.

Record source commit, lockfile/build hashes, environment, schema revision,
gateway/facade version IDs, binding target, secret revision references (no values),
startup and request measurements, and gate outcomes. Keep detailed receipts local
and ignored; publish only sanitized aggregate evidence. An upload timeout is an
unknown outcome until the version inventory and active deployment are reconciled.

Run the same critical reads after promotion. On failure restore the previous
gateway version and its still-valid facade binding, then verify data access.
Recheck secret availability and schema compatibility before promising rollback.
Worker rollback does not reverse Supabase migrations, Access policy, or external
state; see [Cloudflare rollbacks](https://developers.cloudflare.com/workers/versions-and-deployments/rollbacks/).
Production promotion remains a separately authorized operation.

## Implementation order

1. Reconcile current deployed versions and diagnose the facade's actual exception.
2. Make fresh Python bundling and runtime provenance repeatable.
3. Add explicit environment manifests and a single sequential release runner with
   local receipt storage, stage resume checks, and bounded deployment timeouts.
4. Add candidate gateway/facade configuration and existing-contract integration
   qualification, then complete the dedicated-reader cutover through this flow.
5. Connect the same runner to CI once its local execution is proven; CI must not
   create a second competing release path.

No runner, candidate infrastructure, or live deployment is introduced by this
design document. The Python runtime gate must pass before choosing deployment
dates; persistent incompatibility requires an explicit compute-platform decision.
