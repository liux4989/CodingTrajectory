# Datahub Private Cloudflare Hosting

- **Status:** Accepted design; facade implemented locally, not deployed
- **Date:** 2026-09-08
- **Scope:** Access-only hosted Datahub UI and read-only API facade
- **Related:** [remote control plane](remote-ct-control-plane-design.md) and
  [chronicle history](chronicle-history.md)

## Decision

Publish Datahub as a private Cloudflare application. Cloudflare Access protects
the complete application hostname. The hosted application reads only the
bounded, workspace-scoped Chronicle data already approved for remote use; it never
serves host-local evidence or initiates collection, projection, estimation, or
publication writes.

Hosted Datahub is a separate capability mode, not the existing localhost server
placed behind a public URL. The local server continues to own local JSONL,
SQLite materialization, content hydration, and evidence views. The hosted
application consumes the same Supabase-backed public CT contracts as other
remote readers.

The initial release keeps two independent authorization decisions:

1. Cloudflare Access decides whether a browser may reach Datahub.
2. Supabase Auth and RLS decide which CT workspace data that user may read.

Cloudflare identity does not become a Supabase principal implicitly. An identity
broker or service-role bypass is outside this design.

## Target topology

```text
browser
  -> Cloudflare Access (default deny; explicit owner allow policy)
  -> Datahub gateway Worker
       |-> Vite static assets
       |-> verify Cf-Access-Jwt-Assertion for /api/*
       `-> service binding
            -> private CT Python facade Worker
                 -> validate request with existing Pydantic contracts
                 -> pin one Supabase workspace snapshot
                 -> call PostgREST RPC with the user's Supabase JWT
                 -> validate artifact identity, digest, and schema
                 -> execute existing chronicle CT handlers
                 -> adapt the result to the Datahub response contract
```

The gateway Worker is the only publicly routed Worker. The CT facade has no
public route and accepts calls only through its service binding. The raw Access
JWT is not forwarded to Supabase. The user's short-lived Supabase token is
forwarded only to the CT facade and PostgREST, and is never persisted or logged.

The Worker configuration uses the Vite build output as Static Assets, SPA
fallback for client-side routes, and Worker-first routing for `/api/*`. API
routing must not rely on `ctx.access`: Cloudflare currently documents that the
Static Assets router does not pass that context to the user Worker. The gateway
instead verifies the signed `Cf-Access-Jwt-Assertion` header against the Access
issuer and application audience.

## Product responsibilities

| Component | Owns | Must not own |
| --- | --- | --- |
| Cloudflare Access | Browser admission, IdP authentication, allow policy, session policy, audit decision | CT workspace membership or Supabase roles |
| Gateway Worker | Static assets, Access JWT verification, same-origin API boundary, security headers, request limits | Metric computation, CT data storage, service-role credentials |
| CT facade Worker | Pydantic request/response validation, pinned remote runtime, Datahub response adapters | Local discovery, SQLite, raw logs, durable caches, publication or mutation |
| Supabase Auth and RLS | User identity, workspace membership, row authorization | Cloudflare admission policy |
| Supabase CT schema | Chronicle artifacts, revisions, inventory, and approved read RPCs | Raw prompts, responses, commands, event bodies, or host paths |
| Local Datahub | JSONL discovery, SQLite materialization, evidence hydration, local-only pages | Hosted authority or remote fallback |

No Datahub data is copied into Workers KV, D1, R2, Cache API, browser storage,
or Cloudflare configuration. The first release performs request-scoped reads
from Supabase and returns `Cache-Control: no-store` for every API response.

## Authentication and authorization

The Access application covers the complete hostname, not only `/api/*`. Its
policy is default-deny and contains an explicit owner allow rule. Do not add an
`Everyone` selector, bypass rule, reusable service token, or public health path
for the initial release. Exact IdP identities or groups must be inspected before
configuration; group names must not be guessed.

The gateway validates all of the following on API requests:

- the Access JWT is present;
- the signature resolves through the team JWKS;
- the algorithm is the expected asymmetric algorithm;
- issuer, audience, expiry, and required identity claims are valid; and
- the identity is allowed by the configured application policy.

Missing configuration, missing claims, key-fetch failure, and validation failure
all fail closed. The gateway never trusts the Access cookie, email headers, or
arbitrary client-provided identity headers as proof by themselves.

After Access admission, the browser obtains an ordinary short-lived Supabase
user session. The CT facade forwards that bearer token with the publishable key;
RLS and workspace membership remain authoritative. Database passwords,
collector credentials, refresh credentials, and service-role keys are prohibited
from the browser and both Workers. The initial implementation may therefore
show a separate Supabase sign-in after Access; removing that second sign-in
requires a separately reviewed token-broker design.

## Hosted route capability matrix

The inventory below is derived from
`datahub_plugin.serving.routes.ROUTES`. “Adapter” means the remote authority is
safe but the existing localhost response projection is not directly deployable.
The adapter must reuse Python CT handlers and Pydantic response contracts; it
must not reimplement metric semantics in TypeScript.

| Datahub route | Hosted decision | Remote authority or reason |
| --- | --- | --- |
| `GET /api/datahub/snapshot` | Adapter | Pin `ct_workspace_snapshot`; report `source=remote`, `content_scope=chronicle`, and the pinned sequence. Never report local source status. |
| `GET /api/datahub/changes` | Adapter | Compare workspace sequence. A changed sequence invalidates hosted queries as one reset; do not fabricate local entity deltas. |
| `GET /api/datahub/events` | Deferred | Hosted SSE is optional. Use bounded visible-tab snapshot polling first; living and continuous publication require separate verification. |
| `GET /api/overview` | Adapter | Compose `project.list`, `project.sessions`, and bounded graph stats/usage at one pinned sequence. |
| `GET /api/today` | Adapter | Same approved inputs as overview, with a one-day filter and explicit reduced semantic coverage. |
| `GET /api/projects` | Adapter | `project.list`. Never return principal-private agent locations. |
| `GET /api/projects/detail` | Adapter | `project.sessions` plus bounded graph stats/usage at one pinned sequence. |
| `GET /api/sessions` | Adapter | `project.sessions`; titles remain unavailable, while session overview carries bounded turn prose. |
| `GET /api/sessions/timeline` | Omit | No current hosted UI consumer or approved remote projection. Do not expose it as a compatibility route. |
| `GET /api/sessions/context-window` | Prohibited | Context/event evidence is not part of the chronicle remote contract. |
| `GET /api/sessions/graph` | Adapter | `graph.overview` without narrative plus `graph.stats` and `graph.usage`. |
| `GET /api/sessions/tree` | Adapter | `session.tree`. |
| `GET /api/sessions/evidence-timeline` | Prohibited | Depends on host-local evidence identities and hydration. |
| `GET /api/sessions/events` | Prohibited | `session.events` is explicitly local-only. |
| `GET /api/sessions/items` | Metadata only | Permit only `session.items` with `include_content=false`; reject content and do not link it from an evidence view. |
| `GET /api/model-usage` | Adapter | Compose `project.sessions` and `session.model_usage` at one pinned sequence; omit unavailable titles. |
| `GET /api/token-efficiency/project` | Deferred | Requires a reviewed pure Python projection over chronicle stats/usage. Do not port analytical semantics into the gateway. |
| `GET /api/code-time/report` | Adapter | Existing report composition already uses `project.list`, `project.sessions`, and `graph.usage`; refactor it to request-scoped async execution without its thread cache. |
| `GET /api/code-time/forecasts` | Deferred | `estimate.list` authority exists, but hosted estimation reads require current remote verification before exposure. |
| `GET /api/code-time/calibration` | Deferred | `estimate.calibration` authority exists, but hosted estimation reads require current remote verification before exposure. |
| `POST /api/refresh` | Prohibited | A hosted read must not discover local files, publish artifacts, start jobs, or mutate state. Browser refresh only refetches a pinned remote snapshot. |

Unknown `/api/*` paths return JSON `404`. A prohibited route returns a stable
JSON `403` or `404` without revealing whether a local resource exists. Query
parameters use the existing Pydantic bounds; unknown parameters and contentful
options are rejected before any Supabase request.

## Hosted navigation

The frontend receives a build-time `hosted` capability manifest. Hosted mode:

- keeps Sessions and bounded Tree/Graph views;
- enables Today, Compare, and Code Time sections only as their adapters pass
  response parity and privacy gates;
- removes Context and Timeline tabs rather than presenting failing controls;
- removes evidence explorer actions and contentful item links;
- labels the source as a remote pinned workspace snapshot;
- labels missing titles/previews as unavailable under chronicle coverage; and
- never falls back to localhost or silently mixes local and remote data.

Capability checks exist at navigation, API routing, and CT remote dispatch. UI
hiding alone is not a security boundary.

## Runtime compatibility gate

Cloudflare Python Workers currently support Pydantic and ASGI applications but
run on Pyodide, have non-functional threading, and provide only an ephemeral
filesystem. The existing `ThreadingHTTPServer`, `urllib` transport, local SQLite
runtime, thread pools, and process-global caches are therefore not deployable as
written; the hosted path is a request-scoped facade instead.

Before any hosted deployment, the committed facade must demonstrate that it
can:

1. import the required pure CT contracts and handlers;
2. call Supabase through an asynchronous supported HTTP path;
3. validate and replay representative chronicle artifacts byte/digest exactly;
4. execute the approved method matrix without filesystem or thread use;
5. remain within current Worker bundle, CPU, memory, request, and response
   limits; and
6. return the generated Datahub schemas without semantic drift.

Failure stops the Python Worker path. It does not authorize a TypeScript rewrite,
a service-role shortcut, or tunneling the current local Datahub. A full Python
container is a possible later option only after an explicit platform and cost
decision.

## Response and observability policy

Every hosted response includes the pinned workspace sequence and transport
metadata already defined by the remote runtime. Cursor pages remain bound to
that sequence; a continuation from another sequence is rejected or reset rather
than merged.

API responses use `Cache-Control: no-store`. Static fingerprinted assets may use
long-lived immutable caching; the SPA document must revalidate. Add
`X-Content-Type-Options: nosniff`, an explicit Content Security Policy, a strict
referrer policy, and `X-Robots-Tag: noindex`.

Worker logs are aggregate-only: route template, status class, latency, response
size, and generated request correlation. Do not log tokens, headers, email,
workspace/project/session/item identifiers, query values, response bodies, or
Supabase error bodies. User-facing errors are bounded and sanitized.

## Delivery phases

### Phase 0 — compatibility and contract proof

- Record the exact source revision and preserve unrelated work.
- Run the Python Worker compatibility spike against synthetic fixtures.
- Generate the hosted route registry from the authoritative local route list and
  fail if an unclassified route is added.
- Prove every allowed adapter validates against the existing Datahub response
  schema.
- Prove prohibited methods are rejected before transport.

### Phase 1 — local hosted-mode validation

- Build the Vite shell with the hosted capability manifest.
- Run gateway and facade Workers locally with synthetic credentials/data.
- Verify Access JWT validation with valid, absent, expired, wrong-issuer,
  wrong-audience, and forged tokens.
- Verify Supabase authorization independently with member, non-member, expired,
  and missing user tokens.
- Verify direct/deep SPA navigation and that `/api/*` never resolves to the SPA
  fallback.

### Phase 2 — private non-production preview

- Inspect the actual Cloudflare account, hostname, IdP, Access applications, and
  policies before creating resources.
- Create a preview Worker hostname and default-deny Access application with one
  explicit owner allow policy.
- Point the facade only at the already authorized non-production Supabase target.
- Deploy code and configuration without publication or schema mutation.
- Validate signed-out denial, authorized owner access, forged-header denial,
  RLS non-member denial, all approved route schemas, and all prohibited-route
  rejections.

### Phase 3 — production promotion

Production requires separate action-time confirmation. Before promotion, repeat
the target-classification, identity, route, privacy, digest, and schema checks;
record the deployed Worker version and Access application audience. A successful
static deployment alone is not evidence that authenticated API data works.

## Rollback

Keep the previous Worker version deployable. Rollback changes only the Worker
version and, if necessary, disables the Access application route; it never
changes Supabase schema or artifacts. If authentication or privacy validation
fails, disable the hosted application rather than falling back to the local
server, weakening Access, or serving cached data.

## Inputs required before infrastructure changes

The design intentionally does not guess these account-specific values:

- Cloudflare account and zone;
- preview and production hostnames;
- Access team domain and application audience;
- configured IdP and exact owner identity or verified group claim;
- chosen Access session duration;
- Supabase browser sign-in method; and
- authorized non-production workspace identifier.

Resolve and verify them read-only before any Cloudflare resource creation or
deployment.

## Current Cloudflare references

- [Workers Static Assets SPA routing](https://developers.cloudflare.com/workers/static-assets/routing/single-page-application/)
- [Static Assets Worker routing and Access context](https://developers.cloudflare.com/workers/static-assets/routing/worker-script/)
- [Cloudflare Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Cloudflare Access authorization cookie](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/)
- [Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [Python Workers standard-library constraints](https://developers.cloudflare.com/workers/languages/python/stdlib/)
