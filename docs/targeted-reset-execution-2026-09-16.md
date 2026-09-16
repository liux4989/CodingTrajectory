# Targeted workspace reset execution — 2026-09-16

Later evidence: the [2026-09-17 reader recovery](credential-recovery-2026-09-17.md)
found a compatible reader in a separate Keychain service and verified snapshot
zero and empty inventory on the final clean version. The credential-discovery
limit below describes the reset task, not the current reader-access status.

## Final outcome: reset completed

The owner chose no compatibility support for this preproduction environment.
The second attempt used the same approved runtime sources, accepted rejection
of legacy credentials, and completed the reset without translating roles or
replacing CT_PRINCIPALS. The initial rollback is retained below as history.

Exactly one destructive reset invocation returned HTTP 200, `reset: true` and
the exact workspace. No retry occurred. One later reset-shaped teardown probe
was rejected at ingress; it did not invoke the Durable Object reset.

| Stage | Source SHA | Worker version |
| --- | --- | --- |
| Fresh recovery, gate absent | `e9096632d3d6782e770bbf02d7a0e34f56ba3195` | `8ed65c50-1310-4978-8793-609c930b5e01` |
| Gate enabled; reset succeeded | `e9096632d3d6782e770bbf02d7a0e34f56ba3195` | `d431e216-b98a-49b9-978c-3213e5fe1be7` |
| Gate removed | `e9096632d3d6782e770bbf02d7a0e34f56ba3195` | `9890b7de-38e4-4b1a-a0d9-fe2282eb89a9` |
| Clean runtime before secret removal | `2917baea8b980b40c733e155377ab498f5fa9bd9` | `12bfbef0-ac34-4e14-8b00-1d92856691da` |
| **Final clean runtime, recovery secret removed** | **`2917baea8b980b40c733e155377ab498f5fa9bd9`** | **`df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`** |

Final traffic is **100%** on the last version. The secret-only revision retained
the clean script metadata/identity exactly. The clean source files were compared
byte-for-byte against the approved commit. Its inspected bundle contains neither
`ct_workspace_reset`, `CT_RESET_WORKSPACE_ID` nor `CT_RESET_RECOVERY`. The current
documentation commit is an operational record, not deployed source.

### Authentication, state and teardown evidence

| Check | Result | Version |
| --- | --- | --- |
| Recovery authentication before gate activation | HTTP 200; exact workspace; owner present | `8ed65c50-1310-4978-8793-609c930b5e01` |
| Recovery authentication immediately before reset | HTTP 200; exact workspace; owner present | `d431e216-b98a-49b9-978c-3213e5fe1be7` |
| Successful reset | HTTP 200; exact workspace; `reset: true` | `d431e216-b98a-49b9-978c-3213e5fe1be7` |
| Post-reset snapshot and inventory | HTTP 200; snapshot **0**; projects **[]** | `d431e216-b98a-49b9-978c-3213e5fe1be7` |
| Gate-off recovery authentication | HTTP 200; exact workspace; owner present | `9890b7de-38e4-4b1a-a0d9-fe2282eb89a9` |
| Gate-off reset probe | HTTP **503**, `workspace_reset_unavailable` | `9890b7de-38e4-4b1a-a0d9-fe2282eb89a9` |
| Gate-off snapshot and inventory | HTTP 200; snapshot **0**; projects **[]** | `9890b7de-38e4-4b1a-a0d9-fe2282eb89a9` |
| Recovery token after cleanup | HTTP **401**; workspace/owner not established | `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a` |
| Legacy token after cleanup | HTTP **503**; workspace/owner not established | `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a` |

All responses above included matching Worker-version headers. The executing task
was the sole invocation owner. The namespace and object-class bindings matched
the original target throughout. The reviewed reset's object-ID guard remained
unchanged. No other workspace was invoked or reset; no global before/after
workspace census is claimed. ARTIFACTS and its R2 contents were preserved.

### Final secret and credential status

| Name/item | Active status |
| --- | --- |
| CT_PRINCIPALS | Present, unchanged; never exported or replaced |
| CT_CURSOR_KEY | Present; inherited from first upload without rotation |
| CT_RESET_RECOVERY | Removed from final active version |
| CT_RESET_WORKSPACE_ID | Removed before clean deployment; absent in final version |
| Temporary Keychain bearer | Deleted; removal verified |
| Existing Keychain profiles | Unchanged |

Historical uploaded versions remain in Cloudflare version history. Their
temporary recovery records have fixed expirations and the local bearer tokens
have been deleted. Historical secret material was not purged. No secret values,
hashes or principal-registry contents are recorded here.

### Verification limit and separate work

The last authenticated snapshot/inventory check was on the gate-off version,
immediately before clean-code deployment. **No fresh post-cleanup snapshot read
was possible because no compatible application credential remains.** Final
checks prove clean source/version identity, binding preservation, removal of
temporary access, and recovery-token rejection; they do not prove application
usability or upload readiness. The known successful reset result is not ambiguous.

Current-schema credential provisioning and larger publication-limit qualification
remain separate tasks. Latest main was not deployed. No compatibility code,
principal-role mapping, real-data upload, merge or CI change was introduced.

## Historical record: first attempt, before the no-compatibility decision

The remainder describes the initial attempt only. Its legacy-compatibility
blocker and pending-reset status were superseded by the completed second attempt
above; these sections are retained as an audit trail.

Outcome: reset not performed. Recovery deployment was rolled back after the
unchanged existing principal failed the candidate's role validation. Original
production access was restored and verified. No reset gate was enabled and no
reset request was sent.

## Source and deployment identities

| Stage | Source | Worker version | Outcome |
| --- | --- | --- | --- |
| Initial production | Legacy deployed source; exact repository SHA not established | `8c180e59-c074-4d0e-bd08-fee08543592e` | Preflight matched; 100% traffic |
| Approved recovery | `e9096632d3d6782e770bbf02d7a0e34f56ba3195` | `18e61f62-31a6-4dba-b9eb-b6348ce9657f` | Uploaded, bindings checked, deployed at 100%, then rolled back |
| Approved clean source | `2917baea8b980b40c733e155377ab498f5fa9bd9` | None | Dry build only; not deployed |
| Final production | Original legacy version | `8c180e59-c074-4d0e-bd08-fee08543592e` | Restored at 100%; existing authentication verified |

Recovery source is directly based on reviewed reset commit
`9fedcc2bc19d60bdf38f705d344f576be105573b`. Latest main
`145ec3d34be32b0a5ed37cad9369af5fd4612146` was not deployed, rebased or merged.
The recovery worktree was at the exact approved commit with no tracked changes
throughout execution. This documentation commit is a later operational record,
not deployed runtime source.

## Confirmed target and scope

- Account: `b3f2d220197bf66837f1e05c240cf8b1`.
- Worker: `coding-trajectory-control-plane`.
- Origin: `https://coding-trajectory-control-plane.liux4989.workers.dev`.
- Workspace: `fbeca960-ce47-4126-ad21-cca95e1855ae`.
- WORKSPACES: namespace `867b720fe75947219811344c33645219`, class `Workspace`.
- ARTIFACTS: existing `coding-trajectory-artifacts` bucket binding preserved.
- Sole reset invocation owner: this executing task. Invocation count: **zero**.

No Worker, namespace, class or bucket deletion; no CT_PRINCIPALS replacement;
no session-data upload; no CI change; no merge. Production application calls were
connection-status checks, which do not dispatch to a workspace Durable Object.

## Sanitized authentication evidence

| Credential and stage | HTTP | Exact workspace match | Owner present | Worker version |
| --- | --- | --- | --- | --- |
| Temporary recovery on approved recovery runtime | 200 | Yes | Yes | `18e61f62-31a6-4dba-b9eb-b6348ce9657f` |
| Existing credential on recovery runtime | 503 | Not established | Not established | `18e61f62-31a6-4dba-b9eb-b6348ce9657f` |
| Existing credential after restoration | 200 | Yes | No | `8c180e59-c074-4d0e-bd08-fee08543592e` |
| Temporary recovery after restoration | 401 | Not established | Not established | Active legacy version verified through deployment metadata |

The recovery runtime's 503 is `invalid_principal`. Its ingress accepts only
`read`, `collect`, and `owner` role values and rejects the entire principal when
any role is outside that set. In-memory comparison against the existing
credential's successful legacy status response confirmed a role-set mismatch;
no role values, token, digest, registry contents or agent identity were exported.
The same role validation is in the reviewed reset and approved clean sources.
Changing recovery credentials cannot fix this existing-principal incompatibility.

The legacy response does not supply a Worker-version header; final version
identity comes from the authenticated Cloudflare deployment query. Candidate
responses carried matching `X-CT-Worker-Version` headers.

## Secret and gate disposition

| Name or item | Final status |
| --- | --- |
| CT_PRINCIPALS | Preserved unchanged; present on active legacy version |
| CT_CURSOR_KEY | Newly provisioned on inactive recovery version; absent from active legacy version; not overwritten or explicitly deleted |
| CT_RESET_RECOVERY | Provisioned on inactive recovery version; absent from active legacy version |
| CT_RESET_WORKSPACE_ID | Never configured; absent throughout |
| Temporary reset-only Keychain bearer | Generated securely, read-back verified, then deleted after restoration; deletion verified |
| Existing Keychain profiles | Unchanged |

The inactive uploaded recovery version remains in Cloudflare version history.
Its recovery record has an absolute expiration of **2026-09-16 11:55:35.315 UTC**
and the only local bearer was deleted. No claim is made that historical version
secret material was purged. Do not reactivate that version without a new
compatibility assessment. The active legacy version rejects its recovery token.

## Network evidence and its limits

Python and curl initially encountered TLS-handshake failures before application
authentication. Proxy environment variables were absent; macOS HTTP, HTTPS,
SOCKS and automatic proxies were disabled. Those checks do not rule out TUN VPN
routing and do not establish a network root cause.

The system resolved the Worker hostname to `172.19.0.89`. A Cloudflare DNS query
returned public A records `104.21.40.9` and `172.67.173.214`. Route-table inspection
showed **all three addresses use `utun4`**. A curl request pinned to the first
public address succeeded with the original HTTPS hostname and normal certificate
validation. Subsequent application checks used that per-process DNS override.
No proxy, VPN, route table, firewall or certificate-validation settings changed.

This establishes tunnel interception at the host route level. It does not show
whether the VPN subsequently chooses direct or proxy egress, nor prove that the
VPN caused the handshake failures. A per-flow VPN decision would be needed to
resolve that separate question. The HTTP 503 application error was observed
after successful TLS and is independent evidence of principal incompatibility.

## Validation and remaining work

Before production: local qualification and metrics baselines had passed. During
execution, the preserved-binding clean config passed another dry build; its
bundle contained neither reset nor recovery code. Live bindings matched the
approved namespace and bucket before deployment and after restoration.

No post-reset snapshot-zero or empty-inventory evidence exists: reset was never
attempted. The clean source was not deployed. Production currently runs the
restored legacy code, not the new fact runtime.

Before resuming, review the precise compatibility mapping between legacy
principal roles and the candidate's authorization model. Preserve the existing
registry; do not filter or translate unknown roles without reviewing their
semantics and allowed methods. Qualify existing-credential continuity on the
revised recovery and clean runtimes before enabling reset. Any changed runtime
needs a new exact source identity and deployment scope. Real-data upload and
larger publication-limit qualification remain separate jobs.
