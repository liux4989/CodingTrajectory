# Remote Datahub handoff

Prepared 2026-09-10. This is an unfinished implementation handoff, not a healthy
deployment receipt. Read [deployment flow](datahub-deployment-flow.md) and
[private hosting](datahub-cloudflare-private-hosting.md) before continuing.

## Checkout and bootstrap

Repository: `https://github.com/liux4989/CodingTrajectory.git`.
Branch: `codex/datahub-remote-handoff`.

```bash
git clone --branch codex/datahub-remote-handoff --single-branch https://github.com/liux4989/CodingTrajectory.git
cd CodingTrajectory
git rev-parse HEAD
bash scripts/prepare-datahub-remote-handoff.sh
```

The runner needs Git, Bash, uv, Node, npm and Bun, plus package-registry network access.
The local reference toolchain is uv 0.12.10, Node 22.22.0, npm 11.19.0, Bun 1.3.11,
Wrangler 4.129.1 and pywrangler 1.17.2. Python must satisfy the committed
`requires-python` constraints (3.12+). Use `uv.lock` and the web `bun.lock`;
do not upgrade dependencies while reproducing the failure. Wrangler and pywrangler
come from those dependencies. Install the Supabase CLI separately only when
starting the database checks and record its version. Local Supabase migration
replay also requires a supported container runtime.

The runner installs dependencies and performs static/build/metric checks. It
does not need `.env`, database access, Cloudflare login, macOS Keychain, local
SQLite, browser cookies, session logs, prebuilt `python_modules`, or `.wrangler`.
It does not qualify Python Worker packaging or deploy anything. Keep build fan-out
at two jobs. A heavy build on Orb should use a larger Orb selected at task creation;
setup scripts must not resize it.

## Exact work being handed over

The branch includes the unfinished dedicated-reader cutover:

- `datahub_plugin/hosted/transport.py`: server-side password grant, bounded
  responses, ephemeral token reuse, and one reauthentication attempt on 401.
- `datahub_plugin/hosted/worker.py`: reader secret configuration and provisional
  deferred imports from bundled `service` and `transport` modules.
- `web/worker/index.ts`: removes browser Supabase configuration, strips browser
  authorization on facade calls, and restricts connections to the same origin.
- `web/src`: removes the second Supabase login flow.
- Both Wrangler configurations and generated gateway bindings reflect the split.

Paths above are relative to `packages/plugins/datahub`.

The old preview release was committed as `0ae399a`; the release-flow design was
committed as `c1ba6b9`. The previous task reported facade error 1101 after reader
provisioning, and withheld the new gateway. Its first import fix corrected an
older vendored transport copy. Later lazy imports reduced a reported startup
measurement from about 5 seconds to 996 ms, but did not establish a healthy live
response. These are historical observations, not current remote verification.

Do not assume 1101 proves a startup-limit failure. Python deployment snapshots
top-level imports. Inspect actual runtime errors and bundle origins, as explained
in the release-flow document. `scripts/prepare-hosted-worker.sh` currently uses
pywrangler sync followed by installation into `python_modules`; reproducing and
removing ambiguity in that packaging path is the first implementation priority.
Also review token-cache behavior under concurrent requests and secret rotation.

## Remote targets and access

The existing non-production Supabase project ref is `mjwgqmvsvarvcbghhodx`.
The serving gateway is `coding-trajectory-datahub-preview`; the private facade is
`coding-trajectory-datahub-facade-preview`. The entry URL is
`https://coding-trajectory-datahub-preview.liux4989.workers.dev`.
Resolve and verify the Cloudflare account, Access application/policy, current
version IDs, and workspace UUID before mutations. The older version IDs in the
hosting document are historical receipts, not guaranteed rollback targets.

| Configuration | Where it belongs / how to obtain it |
| --- | --- |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | Remote agent secret store; scoped to the target account and required deployment operations |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | Gateway configuration/secrets; resolve the intended Access app before copying to a candidate |
| `CT_SUPABASE_URL`, `CT_SUPABASE_ANON_KEY`, `CT_REMOTE_WORKSPACE_ID` | Facade bindings; existing target configuration, supplied securely when needed |
| `CT_READER_EMAIL`, `CT_READER_PASSWORD` | Facade secrets only; previous task reported these provisioned, but verify presence |
| Supabase management/database credentials | Separate remote secrets only if database administration is necessary; never facade/browser bindings |

Cloudflare secret listing returns names, not recoverable plaintext values.
Existing secrets may suffice for the existing Worker, but a new candidate facade
requires separately supplied values or an explicitly managed rotation. Never
silently recreate the reader because its password is unavailable locally.
Access-policy administration may need a separately scoped token. Database
passwords and management tokens are not runtime read credentials.

Never paste secrets into task messages, shell arguments, Git, logs, or build
artifacts. Do not export this machine's `.env`, Keychain, browser session or
collector identity. No secret values are included in this handoff.

## Ready-to-paste remote task

> Complete the non-production Datahub deployment from the branch
> `codex/datahub-remote-handoff` in `liux4989/CodingTrajectory`. Read `AGENTS.md`,
> `docs/datahub-remote-handoff.md`, and `docs/datahub-deployment-flow.md`. Start by
> recording the checkout SHA and running the portable bootstrap script. You have
> no access to the previous agent's filesystem, browser, Keychain, or caches.
>
> Own the hosted packaging, release runner, candidate configuration, and final
> dedicated-reader cutover. Preserve unrelated work. Use uv and Pydantic, do not
> write unit tests, and keep build fan-out at two jobs. Commit the completed work;
> run the repository metric gate and full baseline workflow as required. Use Cody
> reporting when available, but do not let missing reporting tools block work.
>
> Reconcile actual live Worker versions before any deployment. Diagnose the
> facade's runtime exception from measured evidence, fix reproducible Python
> packaging/import resolution, and implement the staged release flow. Validate
> real Supabase RPC authorization, including write denial, rather than inferring
> read-only access from membership. Keep the seven-day remote scope and existing
> Python CT semantics. No service-role credentials in the application.
>
> Deploy only to the verified non-production environment, using an isolated
> candidate gateway/facade pair. The user authorized completing the private
> preview and dedicated-reader design. Do all read-only checks and a concrete
> dry run before shared mutations; stop at the first failed gate. Do not reset
> the database, broaden Access admission, switch compute platforms, or promote
> production as part of this task. Do not send invitations or other messages.
>
> Validate all seven allowed API routes, prohibited routes, first-request
> behavior, schemas, deep links, and Sessions/Graph data. Final owner Access
> verification needs the user's browser unless an explicitly authorized machine
> identity is available. Never introduce an auth bypass to finish a probe.
> If credentials or browser evidence are missing, finish all independent code
> and synthetic checks, then report the exact remaining requirement. Do not
> claim deployment success based on builds, uploads, or Access redirects alone.
>
> Return the commit, deployed gateway/facade IDs and binding target, validation
> outcomes, preview URL, rollback pair and remaining limitations. Keep receipts
> sanitized and private where they include environment-specific details.

## Acceptance

The work is complete when a fresh remote checkout builds reproducibly, a
repeatable release runner validates an isolated candidate, authorized Sessions
and Graph reads succeed through Access, forbidden reads/writes remain denied,
and promotion plus a viable paired rollback are documented. Until those gates
pass, describe the result as implementation complete or deployment pending,
whichever the evidence supports.

## Preparation evidence

The handoff was checked in a separate macOS checkout assembled from Git content,
with fresh dependencies and no copied local environment files or Worker caches.
The frozen Bun install, Python lint, generated API consistency, gateway type
check, hosted production build, and committed metric baselines are the portable
qualification checks. The original `npm ci` attempt exposed the absence of an
npm lockfile; the bootstrap now uses the repository's committed Bun lockfile.
The metric runner also imports the CLI package, so bootstrap installs the whole
Python workspace rather than only the Datahub package.
The hosted build reports a large chart bundle warning. Linux execution, Python
Worker bundle qualification, and live access checks remain the receiving task's
responsibility. A bounded scan of changed files found no private-key, JWT, or
Supabase-secret token patterns; this is not a comprehensive security audit.
