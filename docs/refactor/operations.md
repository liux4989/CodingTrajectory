# Implemented collection and query workflow

These commands describe the initial implementation, now deployed to the private
Cloudflare workspace. The frozen snapshot site remains independently available.
Replace uppercase identity placeholders with your workspace's assigned UUIDs.

## One connection per role

A collector host can save project defaults and a secure token reference once:

```sh
ct connection configure workstation \
  --role collector \
  --url https://YOUR-AUTHORITY.workers.dev \
  --workspace-id WORKSPACE_UUID --agent-id COLLECTOR_UUID \
  --project-name YOUR_PROJECT --default-source local \
  --token-env CT_COLLECTOR_TOKEN
ct connection check workstation
```

The environment variable's value comes from the host's secret injection mechanism.
Omit `--token-env` on macOS to enter the token privately and store it in Keychain.
The collector token needs `collect`, plus `read` if this agent also queries shared
data. A read-only agent configures `--role reader` without an agent ID and normally
uses `--default-source shared`. A local-only profile uses `--role local` and needs
no endpoint, workspace, or secret.

Existing `ct collector credentials` profiles remain compatible. Query and collector
commands now resolve the same profile. For standard commands place `--profile` and
`--source` before the command; `ct api call` also accepts them after the method.
`CT_CREDENTIAL_PROFILE` and `CT_QUERY_SOURCE` are equivalent environment selectors
for CLI and in-process Core query clients.

## Prepare, publish, and inspect

Run from the collector project's directory:

```sh
ct --profile workstation collector sync --mode prepare
ct --profile workstation collector sync --mode status
ct --profile workstation collector sync --mode publish
ct --profile workstation --source local project sessions
ct --profile workstation --source shared project sessions
```

Prepare and status do not require a token. The canonical journal is committed
before its page is captured into the upload outbox. A restart replays uncaptured
pages and resumes frozen publication attempts. New default delivery databases are
scoped by workspace, collector, and project; `--state-path` in the profile preserves
an explicitly selected state location. Existing custom state paths must be reused
to retain their pending batches.

A profile's local canonical repository supplies available session-scoped metadata
reads at a committed revision. Local-only evidence still comes from source files.
Methods without a canonical repository adapter continue using local source discovery.
A query does not publish or require a working cloud connection in explicit local mode.

## Service policy and recovery

```sh
ct --profile workstation collector sync --mode serve --manual
ct --profile workstation collector sync --mode serve --automatic
ct --profile workstation collector sync --mode pause
ct --profile workstation collector sync --mode resume
```

The initial mode is manual. `--manual` and `--automatic` persist policy; restarting
serve without either flag retains it. These commands do not install a scheduler.
Pause suspends delivery while preparation can continue. Status exposes pending
bytes/resources, age, phases, errors, and consumed/acknowledged progress.

Automatic flush defaults to 60 seconds, 256 KiB of pending encoded data, or 200
changed resources; explicit completion fences also trigger delivery. The service
negotiates missing immutable chunks and uploads bounded requests. Authentication
and definitive conflicts retain pending work with explicit recovery state.

```sh
ct connection rotate workstation --token-env CT_REPLACEMENT_TOKEN
ct --profile workstation collector sync --mode reconcile-remote
ct --profile workstation collector sync --mode publish
```

Use reconciliation only after inspecting the reported reason. Local repository
replacement has a separate `reconcile-local` mode; it preserves attempted batches.
For remote restore, token revocation, or ownership conflicts, consult the status
and reconcile with the authority before replaying incompatible state. Forgetting
a profile does not revoke a server token or delete delivery databases.

## Authority and live Datahub rollout

1. Deploy the additive control-plane implementation with its existing principal
   registry and Durable Object/R2 bindings preserved.
2. For an existing authority, run `ct connection migrate workstation` until
   `complete` is true. Each call performs bounded catalog backfill; new authorities
   initialize an empty ready catalog. No re-publication/reset is required for
   existing metadata. Missing legacy detail projections remain explicitly unavailable.
3. Deploy `packages/plugins/datahub/wrangler.live.jsonc` with Access secrets and
   `CT_CORE_URL`, `CT_WORKSPACE_ID`, and a dedicated `CT_CORE_READ_TOKEN`. Every
   Access-approved reader of this site sees this configured workspace; use a
   separate site/policy when workspace membership differs. Do not use a collector
   credential or expose it to the browser.
4. Verify signed-out denial and authenticated browser reads. Publish a new synthetic
   canary revision and confirm it appears without rebuilding the site.

The live adapter supports projects, sessions, graph/tree projections, bounded
metadata items, snapshots, and changes. Canonical Python functions generate detail
projections, capped at 128 KiB per artifact; missing or oversized detail fails
explicitly. Shared catalog pages are revision-pinned and bounded by rows and bytes; shared
sessions are ordered by observed activity, with artifact identity as a stable tie-breaker.
The named frozen snapshot Worker remains independently available.

## First deployment evidence (2026-09-10)

The [live Datahub](https://coding-trajectory-datahub-live.liux4989.workers.dev/sessions)
uses the owner-only Access policy and a dedicated read-only Core credential.
Authority version: `8c180e59-c074-4d0e-bd08-fee08543592e` (includes reader
provisioning). Live Worker version: `3947e035-57a2-48cf-b666-d4eef3921700`.
Catalog migration completed for 34 existing records.

Verified: collector authentication, reader catalog access, reader write denial
(403), signed-out Access redirects (302), and signed-in Chrome session listing.
A synthetic upload committed after the final site deployment appeared through
normal browser polling: the list increased from 32 to 33 sessions without a
rebuild or page reload. Its graph rendered the expected one session and one turn.
Two tiny synthetic sessions remain in `Deployment-Canary`. The unsupported
context-window route and the shared-source badge need the UI follow-ups listed
in the later checklist.

The deployed runtime exposed a redirect incompatibility missed by the Node-based
live adapter harness. Core requests now use `redirect: manual`; the existing
non-success response check rejects redirects without forwarding credentials.
The live configuration enables `global_fetch_strictly_public` for Worker-to-Worker
HTTP requests. See [Cloudflare fetch guidance](https://developers.cloudflare.com/workers/runtime-apis/fetch/).
Diagnostic logs retain bounded failure categories and HTTP status, not payloads
or credentials. The 11-check adapter qualification and Worker type check passed
after the repair; all four metric baselines still pass.

Rollback: direct readers to the existing
[frozen snapshot](https://coding-trajectory-datahub-preview-candidate.liux4989.workers.dev/sessions).
If the authority itself needs rollback, restore pre-rollout version
`e9bcb1f4-a2a9-4d65-ad04-88c95da9a98e` only after suspending publication and preserving
the current principal registry; that version lacks the live catalog API. Do not
reset Durable Object or R2 data. No collector schedule was activated.

## Qualification after the first deployment

Use the integration scripts listed in [qualification](qualification.md). They
exercise synthetic local endpoints and must not be redirected to production.

This implementation retains the compatibility 8 MiB canonical graph ceiling and
full-prefix reconstruction for changed source sets. Adapter-specific append-only
parsing, manifest-native graphs beyond that ceiling, reference-safe garbage
collection, automatic presence, and sustained multi-host soak remain on the
[later checklist](later-qualification-checklist.md), deferred from the first
deployment by agreement. Storage budgets stop preparation rather than discard pending evidence;
no destructive retention job is enabled. Local runtime qualification does not prove
that a deployed browser, remote host, or credential rollout has succeeded.
