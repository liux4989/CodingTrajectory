# Internal seven-day pilot — 2026-09-17

The user authorized completing the remaining setup and uploading only recent
seven-day sessions, with scale work deferred. The selected build is deployed and
reader access is verified. The reviewed export and replacement credentials are
prepared locally. **Registry replacement and real upload remain pending the
explicit whole-registry grant-retirement decision.**

## Deployed build

| Item | Verified value |
| --- | --- |
| Worker | `coding-trajectory-control-plane` |
| Source commit, Worker and Python contracts | `77f5b44515487e5162a1f4ac86f1829b7563ac32` |
| Bundle SHA-256 | `795582f25b45d20bddc9836a54381286a2f01dbd3e0268c01ba24dca8137b1e6` |
| Active Worker version | `79c38a4d-8985-4774-89d2-828dd3ab1d9c`, 100% |
| Deployment | `b8f339e3-7be2-45d9-b3fe-bcb2306d7e75`, 2026-09-16 18:10:14 UTC |
| Prior version | `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a` |
| WORKSPACES namespace | `867b720fe75947219811344c33645219` |
| ARTIFACTS bucket | `coding-trajectory-artifacts` |

The complete baseline-to-candidate control-plane/collector diff was reviewed.
It introduces normalized SQL staging, transactional staging migration,
publication receipt replay and SQL-backed commit, along with the larger bounds
and byte-aware collector batching. The existing code was deployed without a new
runtime change. The deployment configuration explicitly retained the existing
ARTIFACTS binding, which is absent from the current tracked Wrangler config.
`CT_PRINCIPALS` and `CT_CURSOR_KEY` were inherited unchanged.

The staging migration discards old invisible batches for restaging and preserves
committed facts. Before new publication, the prior version remains a configuration
rollback option. After new-schema writes, prefer a forward fix with immutable
requests and receipts retained; a code rollback does not revert data. No reset.

Validation passed: generated schemas/validators, TypeScript compilation, dry
bundle, four existing local migration-interruption scenarios, and a 12-row local
fixture covering publication, exact read-back, identical retry, permissions and
rejection without partial visibility. The normal nested npm check hit the old
ignored Datahub directory's missing workspace manifest; equivalent generation
and compilation completed successfully from the repository root.

At 18:10:54 UTC, the existing reader received HTTP 200 from connection status,
workspace snapshot and inventory, with the new version and expected workspace.
Its role is exactly `read`, snapshot is **0**, and inventory is **empty**. The CLI
connection check also passed. These are deployed read checks, not a deployed
publication or maximum-capacity qualification.

## Reviewed real-data scope

The export is restricted to the current CodingTrajectory project and the activity
window **2026-09-09 18:07:45 UTC through 2026-09-16 18:07:45 UTC**, retaining complete
canonical graph structure. Modification-time discovery found 31 sources; an
inactive graph outside the actual session-activity window was excluded.

| Prepared export | Count |
| --- | ---: |
| Sessions / source identities | 30 |
| Complete graphs | 29 |
| Fact rows | 29,435 |
| Canonical graph bytes, total | 18,949,906 |
| Largest graph bytes | 4,631,115 |

Narrative content, text previews, session previews and titles were replaced by an
explicit omission marker before recomputing and validating fact hashes. Structural
identities and measurements remain. The final export passed local host-path and
credential-pattern checks and contains no known reader/collector bearer. Raw
sources and the reviewed artifact remain in private local storage outside Git.

The one-off delivery driver uses the existing Pydantic RPC contracts, stages
bounded batches, saves immutable requests before sending, and compares remote
graph digests and row counts to this frozen export. It is prepared but has not
sent real data. The pilot stops on any identity mismatch, rejected request,
unexpected visibility or read-back mismatch. Its limits are 40 graphs, 24 MiB
total and 8 MiB per graph, with one publication in flight. Maximum 16/96 MiB
capacity work remains deferred.

## Remaining credential decision

Keychain holds revision `internal-pilot-2026-09-17-v1`, prepared and read-back
verified: the existing reader and one new `collect` credential preserving the
collector agent UUID. It has not been activated or presented as the current
server registry. The historical registry cannot establish complete preservation.

The pending decision is whether the Worker serves only this internal workspace
and may replace its complete registry with these two grants, retiring every other
old grant. This specific approval is required by the user's earlier instruction
against silently discarding unknown grants; general upload authorization does
not settle that disposition. No further upload approval is needed for the scoped
seven-day export once credentials and the bounded live checks are complete.

The macOS collector launch job is **unloaded** with a **disabled** override. No
matching collector crontab entry was found. No schedule was enabled, no separate
staging environment was created, and the retired Datahub applications remain gone.
