# Chronicle Deployment Readiness — 2026-09-09

- **Result:** Admitted for the designated disposable non-production project
- **Mode:** Authorized CT-only reset, exact committed-schema rebuild, and one
  bounded Chronicle canary
- **Target evidence:** The linked project was reconfirmed active, healthy, and
  non-production; its project reference and credentials are intentionally omitted

## Resolved blocker

The user explicitly authorized the destructive reset after target identity,
ownership, data bounds, and external dependencies were inspected. Preflight
found 25 CT tables containing 7,971 rows, no unrelated public tables, and no
external foreign-key or view dependencies. The authenticated principal was
preserved.

One transaction removed the CT application objects and rebuilt all 25 tables
from the 11 committed migrations. It then restored the workspace owner,
collector registration, and four collector capabilities. The remote migration
ledger now exactly matches the repository, and a linked dry run reports no
pending migrations.

## Admission evidence

The bounded canary published one complete Codex graph through an ordinary
authenticated collector credential. It produced one accepted source checkpoint
and one accepted `ct.chronicle_graph.v1` artifact containing one session, two
turns, and 20 items. No delivery failed, remained pending, was rejected, or had
incomplete graph scope.

A direct database audit found one current artifact revision with a matching
canonical digest, one source-vector entry backed by its accepted checkpoint,
and resource indexes for one session, two turns, and 20 items. Required
Chronicle functions are installed and no retired Shareable function remains.

An exact publication retry returned the original receipt without creating a
revision. A distinct request at the already committed publication sequence was
rejected as stale without changing history. A deliberately retired
`ct.shareable_graph.v1` publication was rejected without creating a revision.

A fresh authenticated remote-only client ran from an empty directory with local
discovery disabled. It verified the artifact identity, strict schema, digest,
and session/turn/item resource lookup, then passed response-schema validation
for all 13 supported historical methods. `session.events`, `session.search`,
and contentful `session.items` failed closed as local-only operations. No
service-role credential was used for publication or reads.

## Boundary

This admission applies only to the reconfirmed disposable non-production
project. It does not authorize the same reset for a retained, shared, or
production database. Continuous collection, a separate remote agent host, and
living or estimation workload qualification remain outside this canary.

The complete sanitized execution record is in
`docs/chronicle-non-production-rollout-2026-09-09.md`.
