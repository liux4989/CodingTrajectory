# Reproducible Cloudflare releases

Implemented locally on 2026-09-10. This workflow has not been run against a
Cloudflare account or GitHub Actions. The earlier deployment evidence in
[operations](operations.md) predates these changes.

The release entrypoint is `uv run python scripts/release-cloudflare.py`. It checks
contracts, native workerd integration, metric baselines and the hosted build;
deploys the authority; resumes bounded catalog migration; deploys Datahub; publishes
one synthetic canary; and verifies scoped reads and the exact deployed versions.
Data publication then uses the existing Worker deployment.

## One-time environment setup

Keep the checked-in production Worker names and separate staging names/bindings in
the two Wrangler configurations. Staging uses its own Worker-owned SQLite DO
namespace and `coding-trajectory-artifacts-staging` R2 bucket. Create that bucket
and provision each environment's Worker secrets and Access policy before release.
This script does not create accounts, grant Access membership, or copy production
principals into staging.

Core requires `CT_PRINCIPALS`. Datahub requires `CF_ACCESS_TEAM_DOMAIN`,
`CF_ACCESS_AUD`, `CT_WORKSPACE_ID`, and a dedicated `CT_CORE_READ_TOKEN` with only
the `read` role. Its `CORE` service binding targets the matching authority.
Collector hosts continue using their external HTTPS endpoint.

[cloudflare/release.json](../../cloudflare/release.json) selects connection profile
names and environment-variable references. Configure `release-staging` and
`release-production` collector profiles with separate environment identities, plus
`reader-staging` and `reader-production` reader profiles. Use the existing
`ct connection configure` workflow and secure token references. The two profiles
for one target must select the same workspace and authority. Set the corresponding
`CT_RELEASE_STAGING_*` or `CT_RELEASE_PRODUCTION_*` variables for `DATAHUB_URL`,
`ACCESS_CLIENT_ID`, and `ACCESS_CLIENT_SECRET`. These are Access service credentials
authorized by the site's policy, separate from Core bearer credentials.

Install locked dependencies with `uv sync --all-packages --frozen`,
`npm --prefix cloudflare/control-plane ci`, and
`bun --cwd packages/plugins/datahub/web ci`.

## Plan, qualify, release and verify

```sh
# Default: display the target and dependency order without network or writes.
uv run python scripts/release-cloudflare.py --environment staging

# Local synthetic qualification; no Cloudflare deployment or provider logs.
uv run python scripts/release-cloudflare.py --environment staging --action check

# Explicit release from committed, clean source.
uv run python scripts/release-cloudflare.py --environment staging --action deploy

# Promote the exact commit/source checked by a verified staging release.
uv run python scripts/release-cloudflare.py --environment production --action deploy \
  --staging-receipt .artifacts/releases/staging-COMMIT_SHA.json

# Read-only live verification using the existing release receipt.
uv run python scripts/release-cloudflare.py --environment production --action verify
```

Receipts default to `.artifacts/releases/ENVIRONMENT-COMMIT_SHA.json`, mode 0600.
Repeating an action resumes completed steps. A changed commit/source fingerprint,
an unexpected active Worker version, or a missing canary stops promotion. A
qualification run made from dirty source cannot authorize deployment; use a fresh
receipt after committing. An explicit `--receipt` path supports isolated runs.

The canary is one synthetic `Release-Canary` session scoped to workspace and
commit. It stays visible as release evidence. Its durable local outbox sits next
to the receipt; do not remove that outbox while a release is interrupted. The
script does not send user session logs as qualification fixtures.

## GitHub Actions

[Cloudflare release](../../.github/workflows/cloudflare-release.yml) is manually
dispatched and uses one job, with environment-level concurrency and no cancellation
of an in-progress release. It invokes the same entrypoint. Run staging first;
production takes that successful staging run ID and requires the same commit.
The workflow checks run provenance before downloading the staging receipt.

Configure GitHub environments named `staging` and `production`, with these values
scoped separately to each environment:

| Variables | Secrets |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID`, `CORE_URL`, `DATAHUB_URL`, `CT_WORKSPACE_ID`, `CT_RELEASE_AGENT_ID` | `CLOUDFLARE_API_TOKEN`, `CT_RELEASE_COLLECTOR_TOKEN`, `CT_CORE_READ_TOKEN`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` |

Worker secrets are provisioned separately; Actions secrets supply deployment and
verification credentials. Only the bounded receipt is uploaded, retained for 30
days. Connection references and canary databases are not uploaded. To recover an
interrupted CI release, download its receipt into a clean checkout of that commit
and rerun the entrypoint with `--receipt`; retain any surviving canary outbox.
Do not simultaneously run a local release for the same environment.

## Verification and rollback

Success requires a read-only principal, write denial, signed-out denial, an
Access-authenticated Datahub response, matching `X-CT-Worker-Version` headers,
and the canary visible through that same Datahub version. A successful deploy
command alone is not success. Local integration runs these checks on real
workerd/SQLite/R2 and a native service binding; it does not establish live health.

The receipt records both new and previous active Worker version IDs. Rollback is
an explicit operator action after checking code/schema compatibility: use the
recorded component version with Wrangler rollback and its matching config/env,
then verify reads with the expected versions. Do not reuse a verified receipt as
proof after rollback. Code rollback does not undo SQL backfill, publications or DO
class migrations. Existing gzip bodies and read paths remain available; no
destructive retention or data reset is part of this release workflow.

The workflow follows Cloudflare's [service binding guidance](https://developers.cloudflare.com/workers/runtime-apis/bindings/service-bindings/)
and [Durable Object deployment constraints](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/with-durable-objects/).
