# Collection and release operating model

Approved direction: 2026-09-10. Implementation evidence is recorded as each step
lands; this document does not itself assert deployment or enable a host schedule.

## Objective

Configure each host once. After explicitly opting into automatic publication,
collection survives restarts and temporary outages, retains pending work, and
updates the shared Datahub without operator intervention. Software releases use
one repeatable workflow. Exceptional identity, authorization, schema, or storage
failures identify the necessary action through one status surface.

## Responsibilities

Keep canonical Python/Pydantic computation on the originating host, durable local
state and immutable outbox attempts, an authenticated Cloudflare upload ingress,
one SQLite Durable Object per workspace, and private immutable R2 objects. Keep
local queries usable offline and remote-bound data restricted to the approved
shareable representation.

The workspace object owns authorization, source fences, committed manifests,
revision order and read indexes. Payload decoding, hashing and validation belong
outside its short commit path. R2 writes precede SQLite references; the two stores
do not share a transaction. Verified staging must bind immutable content to the
authenticated principal and canonical/projection versions.

Use a service binding for Datahub-to-Core calls. Preserve the dedicated reader
credential during the transport change. A future credential-free internal
entrypoint must enforce its own read-only workspace scope; a binding alone does
not authorize arbitrary client-selected workspaces or write methods. External
collector hosts continue using scoped bearer authentication over HTTPS.

Cloudflare recommends [service bindings between Workers](https://developers.cloudflare.com/workers/runtime-apis/bindings/service-bindings/)
and [SQLite-backed Durable Objects](https://developers.cloudflare.com/durable-objects/best-practices/access-durable-objects-storage/).
Add Queues, Workflows, D1 or sharding only for a demonstrated workload or recovery
requirement. Do not duplicate the existing authority merely to add a product.

## Host lifecycle

Provide one managed service per user/host, with registered project directories,
connection profiles and pinned delivery-state paths. Reuse the existing per-project
canonical repositories and outboxes; registration and upgrades never reset them.
Check identities when profiles change. Project failure must not prevent unrelated
projects from making progress. Preparation and delivery have independent bounded
execution lanes.

Installation writes a supervisor template without registering an automatic start.
Explicit enablement registers the service with launchd on macOS or systemd on
Linux. Publication remains manual unless automatic mode is explicitly selected.
Restart retains publication policy and pause state. Disable stops supervision;
removing a project or installation preserves its data and credentials.

Tokens remain in Keychain, the supervisor environment, or an explicitly selected
private secret file. Service manifests contain references, never token values.
Rotation is read on the next delivery attempt. Preparation and status require no
working credential. Network retry and lost-ACK recovery reuse frozen batch IDs;
ownership transfer, database restore and incompatible schemas require explicit
reconciliation. Do not auto-reset a database to clear an error.

## Release lifecycle

Use one checked-in release entrypoint that can run through Workers Builds or CI:

1. Validate generated contracts, Worker types and relevant integration scenarios.
2. Deploy to an isolated staging authority and Datahub with separate DO/R2 state.
3. Resume bounded catalog migration until complete; stop on bounded errors.
4. Verify authenticated reads, reader write denial and signed-out denial. A
   synthetic publication must become readable without another website deployment.
5. Deploy compatible production authority and Datahub versions in dependency
   order and record versions, verification results and rollback constraints.

Validation is read-only by default; release and fixture publication are explicit
actions. Never direct general synthetic qualification suites at production.
Keep staging and production resource bindings explicit and credentials separate.
Record only bounded counts, versions, hashes and failure categories in receipts.

[Workers Builds supports custom build/deploy commands](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/).
Workers implementing Durable Objects do not get ordinary [branch preview URLs](https://developers.cloudflare.com/workers/ci-cd/builds/build-branches/);
use a separately deployed staging environment. Durable Object class lifecycle
changes are atomic and have [additional rollback constraints](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/with-durable-objects/).
Application SQL backfills and code rollback are separate from those class changes
and from restoring stored data.

## Status and acceptance

Expose collection progress, pending count/bytes/oldest age, next retry, last
acknowledged publication, publication policy, service heartbeat and bounded
recovery instructions. Distinguish a live local service, reachable cloud authority,
committed publication freshness and observed session activity. A stale or absent
collector report means unknown freshness, never a manufactured ready state.

| Operation | Required result |
| --- | --- |
| Register a host/project | One setup flow; no implicit upload or scheduling |
| Enable automatic publication | Explicit choice; retained after restart |
| Restart or temporary outage | Retained batches drain without duplicate effects |
| One blocked project | Other independent projects continue |
| Rotate a credential | Same identity/outbox; next eligible attempt uses the new secret |
| Publish data | Shared reads advance without software deployment |
| Release software | One validated, resumable procedure with bounded evidence |
| Diagnose failure | One status surface shows affected project and required action |

Qualify with synthetic integration fixtures, real local runtime and process
interruption, without new unit tests. Build/test fan-out stays at two jobs. Keep
metrics gates and committed expected values intact. A successful local check does
not establish live deployment health. The 8 MiB graph ceiling, adapter-specific
incremental parsing, destructive cleanup and sustained multi-host capacity remain
explicit limits until their separate qualification passes.
