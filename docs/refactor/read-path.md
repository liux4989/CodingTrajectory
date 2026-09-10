# Local and shared query architecture

## Same contracts, explicit authority

Use the existing Core protocol and `ct.datahub.v1` at
`POST /api/datahub/query`. Core owns canonical queries/measurements; Datahub owns
presentation and enrichment. Do not merge their namespaces or duplicate their
metrics calculations. Both local and shared adapters validate against the same
applicable Pydantic request/response contracts.

Local API calls read the host repository. Shared calls authenticate the workspace
and read committed published versions. No query starts a publication. Remove the
`CT_AUTO_PUBLISH`/`before_read` coupling from the ordinary read path as an explicit
migration step; retain an explicit publication command for users who need it.

A browser's physical location does not choose the data source. Local and shared
selection is explicit. Cache identity includes source, workspace, revision, method,
filters, and schema/projection version. A source switch invalidates incompatible
cached results and keeps navigation only where capabilities allow it.

## Query execution

1. Resolve the authenticated source and its advertised capabilities.
2. Select a committed read revision and bind it to the request/page cursor.
3. Use indexed catalog projections for project/session lists and bootstrap data.
4. Fetch only the requested graph/turn/item manifest or projection on navigation.
5. Validate returned identities, hashes, versions, coverage, and availability.
6. Poll the cheap published watermark; fetch a bounded change page when it moves.
7. Invalidate affected resources. If history expired, perform an explicit resnapshot.

A historical revision token is not a frozen deployment. It provides consistent
pages while new publications continue; refresh selects a new token. Heartbeats
and estimator activity do not force historical pages to reset. A living request
also fixes an evaluation instant for lease freshness.

## Native hosted adapter and canonical projections

Keep the hosted query adapter in a native Worker. It authenticates Access/browser
identity, derives authorized workspace membership, and invokes internal scoped
read operations through a server-only boundary. Collector bearer credentials
must never be sent to the browser. Authentication at the website and authorization
at workspace/resource access are both required.

Generate shareable domain read projections on the originating host with existing
canonical Python functions. Store their schema/version and canonical dependency
hash. Native Worker adapters translate these projections into Datahub's response
shape; they do not port token/cost/runtime formulas independently. Extend the
canonical projection registry when a field requires computation not already
available. Datahub-specific builders belong in the plugin and consume Core
contracts; Core must not import Datahub UI models.

Required list projections are committed atomically with their canonical revision.
A derived detailed projection can be explicitly unavailable/rebuilding, but never
combined with canonical data from a different revision. Do not introduce a Python
facade deployment as an implicit fallback for missing Worker functionality.

## Capability coverage

| Method family | First live adapter | Final supported behavior |
| --- | --- | --- |
| Capabilities, snapshot, changes | Indexed metadata and publication feed | Same, with retention-aware resets |
| Projects and sessions | Existing shareable fields, bounded pages | Cross-host published inventory with ownership/provenance |
| Graph and tree | Scoped projections within compatibility budgets | Manifest-native/paged resources for large graphs |
| Metadata items | Existing bounded metadata | Lazy pages pinned to the parent revision |
| Local raw content/search/events | Explicitly unavailable remotely | Remain local-only under this scope |
| Usage/overview/analysis | Advertise only qualified methods | Canonical projection/reduction parity before enabling |
| Estimation | Preserve existing contracts separately | Never inferred from missing historical data |

Cross-host aggregates must account for duplicate session identities, overlapping
graphs, projection-only items, unavailable usage, and non-additive measurements.
Summing displayed per-host totals is not an acceptable aggregation implementation.
Qualify each aggregate against the shared canonical reducer before advertising it.

Large-graph paging may require additive method parameters or a versioned resource
query. Freeze that Pydantic contract in R5 before UI work; preserve existing small
graph calls and return explicit budget errors instead of truncation. The public
transport envelope can stay stable while a method's schema is versioned explicitly.

## UI state

Show publication mode and last published timestamp separately from session state.
Distinguish complete/partial/unavailable/unsupported using the existing availability
envelope. Loading a session is lazy; it does not imply uploading it. Offline local
queries remain usable. Remote reads show the last valid committed data and its age,
not a guessed empty workspace. Feature capability is reported by the adapter, not
inferred from a build flag or a label saying remote/local.

Website code changes require deployment. Session publication does not. Frozen
static exports are no longer supported. Revision snapshots still pin consistent
reads; an unavailable live authority is reported explicitly.
