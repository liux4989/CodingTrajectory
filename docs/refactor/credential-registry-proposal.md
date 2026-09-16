# Credential lifecycle and registry management

Status: design proposal only, 2026-09-16. No credentials issued, secrets changed,
deployments performed or uploads authorized by this document. No legacy-role
translation, compatibility layer or temporary recovery mechanism is proposed.

## Starting point

The reset execution record identifies clean source
`2917baea8b980b40c733e155377ab498f5fa9bd9` and final Worker version
`df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`. These are the last verified deployment
identities, not a fresh live check for this design. Snapshot zero and empty
inventory were observed before clean-code deployment. Existing credentials are
incompatible by the owner's explicit preproduction decision.

Target: Worker `coding-trajectory-control-plane`, account
`b3f2d220197bf66837f1e05c240cf8b1`, origin
`https://coding-trajectory-control-plane.liux4989.workers.dev`, workspace
`fbeca960-ce47-4126-ad21-cca95e1855ae`. Preserve WORKSPACES namespace
`867b720fe75947219811344c33645219`, class `Workspace`, ARTIFACTS and CT_CURSOR_KEY.
No reset/recovery binding or code is to be reintroduced.

## Decisions proposed

1. Keep the current digest-to-principal registry and current protocol. Separate
   Cloudflare administration from application access; do not build an issuance API.
2. Bootstrap a reader first, obtain a fresh empty-workspace check, and stop there
   until collector issuance is approved. Leave collection schedules paused.
3. Give each collector installation a separate token and stable agent UUID.
   Rotate its token without changing its agent identity or pending work.
4. Keep owner access exceptional. Do not issue an owner bearer merely to edit
   CT_PRINCIPALS: that operation uses Cloudflare account authorization.
5. Maintain the complete intended registry in an encrypted owner-controlled
   secret store. Git holds the schema, procedure and sanitized change evidence,
   never the registry, bearer tokens or token digests.

## Grants and actual enforcement

| Credential | Server role | Enforced scope | Recommended use |
| --- | --- | --- | --- |
| Reader | `read` | One workspace; all allowed read methods in that workspace | Fresh snapshot/inventory checks and later shared reads |
| Collector | `collect` | One workspace; write requests must match its agent UUID; source fencing also applies | One collector installation; no read role by default |
| Owner | `owner` | One workspace; implies both read and collect, with write agent checks | Optional operator-only credential; never installed in a collector |

At the pinned clean source, all valid principals can call `ct_connection_status`.
Read methods are `ct_workspace_snapshot`, `ct_project_inventory_snapshot`,
`ct_fact_read` and `ct_remote_living`. Collector methods include registration,
staging, publication, recovery and heartbeat. There is no owner-only principal
administration endpoint and no reset endpoint in the clean runtime.

These are workspace-level grants, not project-level or field-level permissions.
A reader is not restricted to inventory metadata. Provisioning must not advertise
narrower authorization than the runtime actually enforces. Profile role labels
are client intent, not security grants; existing profile schema has no `owner`
variant. An optional owner credential belongs in a separate operator Keychain
entry, not a new unsupported profile role.

Every registry entry requires a workspace UUID, agent UUID and role list. Reader
entries also need an agent UUID even though reader profiles may omit it. The
provisioning validator should require canonical UUIDs, a SHA-256 digest key,
nonempty unique supported roles and no unexpected fields. This stricter issuance
policy is not a claim that the runtime already validates every such condition.

## Resolve registry authority before any secret write

CT_PRINCIPALS is one Worker-wide secret containing all principal entries. Updating
one target grant requires writing a complete replacement secret value. Dashboard
and ordinary secret-list APIs do not reveal the existing value. A successful
status response for one credential does not enumerate the registry.

**Preferred path:** locate an authoritative encrypted copy of the existing
registry. Construct the proposed value privately; preserve entries outside the
target workspace byte-for-byte and explicitly retire target legacy grants. Show
only counts, affected workspace scope and intended capabilities for approval.
Do not map unknown roles. Unknown target grants are retired only as explicitly
approved; outside-target entries are neither normalized nor removed.

**If no authoritative copy exists:** a scoped preservation update cannot be
proven. Do not extract secrets by deploying an export endpoint or silently replace
the registry. Ask the owner to establish whether this Worker is exclusively for
the target workspace. Only an explicit whole-registry rebootstrap authorization,
including retirement of every old grant, would permit starting from a new
target-only registry. Preproduction status alone does not prove single tenancy.

The original prohibition on replacing CT_PRINCIPALS remains a separate action
gate. Approval of this design or the earlier reset does not authorize that write.
The concrete approval item must identify the complete registry's affected scope,
reader/collector/owner grants to issue and old grants to retire.

## Issuance, activation and rollback

| Phase | Required behavior and evidence |
| --- | --- |
| Prepare | Generate a cryptographically random bearer, store directly in Keychain or the selected host secret manager, and verify read-back in memory. Do not use chat, argv, clipboard, logs or repository files. |
| Assemble | Build and validate the registry privately with Pydantic. Keep the full registry and rollback copy encrypted. Record a non-secret revision ID and scoped grant-count change, not digest values. |
| Preflight | Recheck account, origin, live version, binding identities and registry revision. One registry writer owns the operation. Stop on drift; a local lock alone does not fence other administrators. |
| Stage | Upload a secret-only Worker version through a secure pipe. Preserve the clean code and every unrelated binding/secret. Verify unchanged script identity, target account and resulting secret names. |
| Activate | Recheck live version immediately before assigning traffic. Activate the explicitly inspected version at 100%; do not split credentials across mixed registry versions. Record source and deployed version. |
| Verify reader | Require HTTP 200, exact workspace, expected role, expected agent and version. Read snapshot and inventory without registering or publishing anything. Require zero/empty; if not, stop and investigate rather than reset again. |
| Roll back | Before any writes, restore the inspected prior version if new access fails. This restores configuration, not workspace data, and may restore legacy incompatibility. Do not claim prior credentials become valid on the new runtime. |

Secret values omitted from an additive upload are preserved, but CT_PRINCIPALS
itself is an entire value, not a server-side per-entry patch. Never confuse those
two operations. Validate the final registry against the complete intended state.
Before rollback, check whether the old version would reactivate a revoked grant;
do not undo a security revocation merely to restore a working configuration.

## Rotation, revocation and custody

- Rotation: stage a new token with the same workspace, agent and roles; retain
  the old digest for an explicitly bounded operational overlap; verify the new
  token; switch the client secret reference; remove the old entry; verify 401.
  Pending immutable batches, receipts, checkpoints and collector state remain.
- Revocation: remove only the approved digest entry and activate the resulting
  registry version. Deleting a local profile or Keychain item is not server-side
  revocation. Previously committed data is not deleted by revocation.
- Expiry: the pinned runtime has **no ordinary-token expiry enforcement**.
  A review/rotation due date is an operational reminder, not a TTL. Do not add
  ignored expiry fields and claim automatic revocation. Enforced TTLs would need
  a separately scoped runtime change.
- Custody: raw tokens are not shared among hosts. Owner credentials stay out of
  browser bundles, collectors, CI logs and exported artifacts. Headless hosts use
  their existing secret injection mechanism, not checked-in environment files.
- Recovery: the encrypted intended registry plus Cloudflare account authority
  allow issuing a replacement bearer. Hashes cannot recover a lost bearer.
  Do not revive the reset recovery secret or weaken authentication.

## Acceptance and open choices

On synthetic local/staging fixtures, qualify correct and wrong workspace/agent,
reader-write denial, collector-read denial, malformed/revoked credentials,
rotation overlap and old-token rejection. Rejected writes must not advance the
snapshot. Current-profile checks and local-only operations must never publish.
Do not build a legacy compatibility matrix. Use actual-runtime qualification;
no unit tests are requested.

On the reset target, start with positive read-only checks only. Negative write
probes belong in staging so a faulty permission check cannot write to the target.
Report sanitized status, identity matches, capabilities and version; retain no
contentful response payloads.

Before execution, settle: authoritative registry availability and whole-Worker
scope; reader-only bootstrap versus later collector issuance; and the intended
collector host/agent identity. Owner issuance is optional, not a prerequisite.

Sources: pinned clean `src/index.ts` and `connections.py`; existing
[connection lifecycle](connections.md); [reset execution record](../targeted-reset-execution-2026-09-16.md).
Next: [upload qualification plan](upload-qualification-plan.md).
