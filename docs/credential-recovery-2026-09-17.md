# Production reader and registry recovery — 2026-09-17

Later scope update: the legacy Cloudflare Datahub deployments were
[retired](datahub-retirement-2026-09-17.md), and the
[simplified internal rollout](refactor/upload-qualification-plan.md) was accepted.
The reader evidence below remains valid; Datahub is no longer a required client.
Registry setup now follows one coordinated cutover in the
[internal credential design](refactor/credential-registry-proposal.md). The
findings below record the recovery investigation before that retirement.

Outcome: **reader access and fresh empty-state verification completed**. A
historical registry reconstruction is encrypted in macOS Keychain, but its
equivalence to the current Worker-wide secret is **not established**. Registry
replacement remains pending scope confirmation and explicit grant disposition.

Checks ran on 2026-09-17 Asia/Shanghai (2026-09-16 UTC). No Worker version,
Cloudflare secret, registry grant, namespace or bucket was changed. No reset,
registration, publication, heartbeat or negative write probe was sent.

## Verified production identity and access

| Check | Result |
| --- | --- |
| Worker | `coding-trajectory-control-plane` |
| Account | `b3f2d220197bf66837f1e05c240cf8b1` |
| Active version before and after checks | `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`, 100% traffic |
| Recorded deployed source | `2917baea8b980b40c733e155377ab498f5fa9bd9`; no new build or deployment |
| Workspace | `fbeca960-ce47-4126-ad21-cca95e1855ae` |
| WORKSPACES | `867b720fe75947219811344c33645219`, class `Workspace` |
| Preserved secrets | `CT_PRINCIPALS`, `CT_CURSOR_KEY` |
| Preserved ARTIFACTS binding | `coding-trajectory-artifacts` |
| Reader connection | HTTP 200; exact workspace and agent; role exactly `read`; protocol `ct.core.v1` |
| Fresh workspace snapshot | HTTP 200; sequence **0** |
| Inventory pinned to sequence 0 | HTTP 200; projects **[]** |
| Different workspace connection check | HTTP 403, `workspace_denied`; ingress-only status method |
| Final workspace snapshot | HTTP 200; sequence **0** |

Every application response above carried the expected Worker-version header.
Response models were validated with Pydantic. Reader-write denial remains a
staging qualification item; no write-shaped production request was used.

The earlier statement that no compatible credential remained was too broad.
The configured `default` and `cloudflare` profiles both return HTTP 503
`invalid_principal`. The separate Keychain service **CodingTrajectory Cloudflare
cutover**, account **datahub-live-reader**, contains an existing compatible reader.
The cutover collector and estimator credentials still return `invalid_principal`.

The existing reader was configured on this same Mac as **production-reader**,
with role `reader`, source `shared`, and its verified workspace/agent identity.
Its bearer is stored under the normal macOS Keychain profile service; secure
read-back and the CLI connection-check handler passed. Existing profiles were
not overwritten. This reuses a credential; it does not issue a new independent
grant or rotate the Datahub reader. No tokens or token digests are in this record.

## Registry search and recovered evidence

The search covered project/configuration locations, relevant worktree artifacts,
matching macOS Keychain service metadata, and the original provisioning task.
The local development registry has no target-workspace entries and matches none
of the saved production tokens. It is not a production recovery source.

The original task's final provisioning recipe reconstructs three entries from
the cutover Keychain tokens: one reader and two legacy principals, all for the
target workspace. The original reader agent matches the freshly authenticated
reader. The old plaintext principals artifact is absent.

An immutable reconstruction was saved and read-back verified in Keychain:

- Service: **CodingTrajectory principal registry recovery v1**.
- Account: **coding-trajectory-control-plane:2026-09-10**.
- Status: **historical_reconstruction_not_verified_current**.
- Source task: `01a0872d-28f1-7601-bf58-054595412db3`, provisioning record 3180.
- Corresponding Cloudflare audit event:
  `01a08a0a-f101-7e3a-be51-a6815f693f62`, 2026-09-10 06:39:28.001 UTC.

The archive preserves the legacy role lists without translating or dropping
entries. It is historical recovery evidence, not an approved upload payload.
The full value and provenance are encrypted in Keychain, outside Git.

The account audit query for this Worker, from 2026-09-10 through this check,
returned 33 events without a continuation cursor. Its two named secret-put
events targeted `CT_PRINCIPALS`; the later version operations match the recorded
reset/recovery timeline. However, version-upload audit records omit request
bodies, and secret metadata does not reveal the registry value. This evidence
does not prove that a reconstruction exhausts the current registry.

The namespace listing completed in two pages and returned one stored object.
An object count does not enumerate grants or establish exclusive tenancy.

## Remaining decision and deployment gates

Before replacing `CT_PRINCIPALS`, establish either an authoritative complete
current registry or owner-confirmed exclusive workspace scope with explicit
approval of the complete replacement and old-grant retirement. The current
reader may depend on other clients, so include its disposition in that review.
The ownership question remains unanswered as of this record.

Any eventual registry change must preserve the current code, `CT_CURSOR_KEY`,
namespace and bucket; inspect a staged secret-only version before activation,
then repeat identity and positive state checks. Do not upload the historical
reconstruction as if it were current or use a reset to recover access.

No collector credential was issued, schedule enabled, staging resource created,
larger-limit runtime qualified or promoted, or real data uploaded. Schedule state
was not independently re-audited in this task. Continue with the separately
approved [upload qualification plan](refactor/upload-qualification-plan.md).
