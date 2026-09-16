# Targeted reset: temporary owner recovery

Status: **reset completed**, after the owner explicitly chose no compatibility
support for this preproduction environment. The approved recovery source
`e9096632d3d6782e770bbf02d7a0e34f56ba3195` performed one successful targeted reset.
Snapshot zero and empty inventory were verified after reset and after gate removal.
Clean source `2917baea8b980b40c733e155377ab498f5fa9bd9` is now deployed as
`df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`. Temporary reset/recovery code, active
bindings and the local recovery credential were removed. CT_PRINCIPALS was
preserved without role mapping or compatibility support. Legacy credentials
used by the reset task remain incompatible. A separate existing reader was
[recovered and verified on 2026-09-17](credential-recovery-2026-09-17.md), including
snapshot zero and empty inventory on the final clean version. See also the
[reset execution record](targeted-reset-execution-2026-09-16.md).

## Immutable target and source boundaries

| Item | Value |
| --- | --- |
| Repository | `liux4989/CodingTrajectory` |
| Account | `b3f2d220197bf66837f1e05c240cf8b1` |
| Worker | `coding-trajectory-control-plane` |
| Origin | `https://coding-trajectory-control-plane.liux4989.workers.dev` |
| Workspace | `fbeca960-ce47-4126-ad21-cca95e1855ae` |
| Existing WORKSPACES namespace | `867b720fe75947219811344c33645219` |
| Existing class | `Workspace` |
| Last verified live version | `8c180e59-c074-4d0e-bd08-fee08543592e` at 100% |
| Original reviewed reset candidate | `9fedcc2bc19d60bdf38f705d344f576be105573b` |
| Reset candidate parent / clean runtime source | `2917baea8b980b40c733e155377ab498f5fa9bd9` |
| Excluded latest main | `145ec3d34be32b0a5ed37cad9369af5fd4612146` |

The recovery commit is a direct child of the reviewed reset candidate. Its reset
implementation, storage, contracts and publication limits remain byte-identical
to that candidate. There is no rebase, merge, CI change or upload-readiness claim.

The production version's bindings were read without returning secret values:
`WORKSPACES` targets the namespace above, `ARTIFACTS` targets the existing
`coding-trajectory-artifacts` R2 bucket, and `CT_PRINCIPALS` is a secret binding.
Preserve all three. Do not remove the R2 binding just because the reviewed new
runtime does not use it. No bucket, namespace, class or Worker deletion is allowed.
Recheck live state immediately before any eventual deployment.

## Recovery mechanism and limitations

`CT_RESET_RECOVERY` is an optional secret containing exactly:
`workspace_id`, `agent_id`, `token_sha256`, `not_before_ms`, `expires_at_ms`.
It stores only the SHA-256 token digest, not the bearer token. The code pins the
workspace above and enforces a maximum lifetime of one hour. Provision a shorter
30-minute window for the operation. Invalid, expired, future-dated, overlong or
wrong-workspace records do not authenticate. Validation happens on each request.

The existing `CT_PRINCIPALS` lookup runs first. Existing credentials keep their
existing roles even if their digest appears in the recovery binding. A missing or
malformed primary registry still fails closed; recovery cannot replace it.

The temporary principal reports `owner` but can call only:

- `ct_connection_status`
- `ct_workspace_reset`, still requiring the reviewed gate and exact confirmation
- `ct_workspace_snapshot`
- `ct_project_inventory_snapshot`

Publication, registration, collector recovery, living reads and fact reads are
denied for this credential before object dispatch. This is temporary operational
authority, not a general owner credential for future application use. Inventory
access is limited to the target workspace; request it only after snapshot zero.

The bearer token will be generated with `secrets.token_urlsafe(48)` and stored
directly through the existing macOS Keychain backend under a distinct reset-only
service/account. Do not overwrite `default` or `cloudflare` profiles. Generate a
new UUID for its agent identity; it needs no collector registration. Confirm a
successful Keychain read-back in memory before provisioning its digest.

Generate a missing `CT_CURSOR_KEY` independently with cryptographic randomness.
Supply new secret values to Wrangler through a captured subprocess stdin pipe;
never use arguments, shell interpolation, logs, committed files, chat, clipboard,
or browser forms. Only return secret names and status. Preserve `CT_PRINCIPALS`
without reading, exporting, rewriting or replacing its value.

## Required revised deployment scope

The original instruction permits only the exact `9fedcc2` source. That runtime
does not recognize a recovery binding; adding a secret alone cannot restore
access. Executing this plan therefore requires approving the exact new recovery
SHA plus the clean runtime source below. The original independent review of
`9fedcc2` does not cover the authentication changes. This proposal has local
qualification and an author security review, not a new independent review.

The required exception to the original sequence is: deploy recovery-capable code
with the reset gate absent, then establish owner authentication. The reset gate
must remain absent until that authentication succeeds. Do not mutate production
under the old exact-SHA authorization.

## Execution after revised source scope is approved

1. Assign this executing task as the sole invocation owner. Recheck the account,
   live version, bindings, exact source commit, clean worktree and reviewed diff.
   Stop on changed deployment state or target/effect drift. Do not touch main.
2. Build from the approved recovery SHA using its own generated contracts and
   lockfile-pinned dependencies. Use the committed `wrangler.reset-ops.jsonc`,
   retaining the existing ARTIFACTS binding and pinning the account.
   Keep the existing WORKSPACES class and v1 migration; do not create migrations.
   Run a dry build, inspect the sanitized binding plan, and verify no reset gate
   is present. Do not use an npm deploy shortcut that deploys a different checkout.
3. Issue the temporary Keychain credential and provision only `CT_RESET_RECOVERY`
   and the missing `CT_CURSOR_KEY` alongside the code using `wrangler versions
   upload --config wrangler.reset-ops.jsonc --secrets-file /dev/stdin`. Supply the
   JSON secret map through a captured subprocess stdin pipe, not a disk file.
   Omit `CT_PRINCIPALS` entirely: the additive upload preserves existing secrets.
   Inspect returned version metadata before assigning traffic; required secrets
   must all exist and the namespace/bucket identities must match the pinned
   values. Record the recovery SHA, deployed version and expiration without
   credential material. Deploy 100% to the approved recovery version with the
   reset gate absent; do not split traffic with the legacy runtime.
4. Through a process that reads the token directly from Keychain, send the exact
   `ct.core.v1` / `ct_connection_status` request with `id: null` and only the target
   `workspace_id`. Require HTTP 200, `ok: true`, exact workspace equality, `owner`
   role and the expected `X-CT-Worker-Version`. Report only those sanitized checks.
   The first run also required legacy credential continuity and rolled back when
   it failed. The owner's subsequent preproduction/no-compatibility decision
   accepts legacy rejection. Preserve the original credential and registry without
   mapping legacy roles. Recovery-owner authentication must still pass every
   target and version check before enabling reset.
5. Enable `CT_RESET_WORKSPACE_ID` only for the pinned workspace using the same
   reviewed recovery source. Verify the resulting version and bindings before
   invoking. Keep the credential within its validity window.
6. The sole invocation owner sends exactly one reset request:

   ```json
   {
     "protocol": "ct.core.v1",
     "id": null,
     "method": "ct_workspace_reset",
     "params": {
       "workspace_id": "fbeca960-ce47-4126-ad21-cca95e1855ae",
       "confirmation": "reset:fbeca960-ce47-4126-ad21-cca95e1855ae"
     }
   }
   ```

   Do not run this through concurrent tasks or automatic retries. This reset has
   no durable idempotency receipt. If transport fails or times out, inspect the
   target snapshot and inventory before deciding whether a retry is justified;
   do not infer failure from a missing HTTP response. An empty target may prove
   the desired state without proving which invocation caused it. Record that
   uncertainty if the reset result is unknown. Do not reseed or upload anything.
7. Require a matching version and successful reset response, then snapshot zero
   and empty project inventory at snapshot zero. Sanitize response output. The
   scope evidence is the reviewed object-ID guard, local wrong-object rejection,
   unchanged namespace binding and target identity. Do not claim a production
   census of other workspaces; this task has no authority to inspect/reset them.
8. Remove `CT_RESET_WORKSPACE_ID` immediately while keeping the same recovery
   code. Before the credential expires, send the same reset request and require
   `workspace_reset_unavailable` / 503; recheck snapshot zero. If it has expired,
   a 401 proves only credential rejection, not the gate's response, so use the
   deployment binding absence as gate evidence and report the limitation.
9. Deploy clean runtime source **`2917baea8b980b40c733e155377ab498f5fa9bd9`** using
   a freshly validated overlay that preserves WORKSPACES, ARTIFACTS, CT_PRINCIPALS
   and CT_CURSOR_KEY and omits the reset gate. This is exactly the reviewed reset
   candidate's runtime before its temporary reset patch, not latest main. Build
   its contracts from that source. Prove the temporary reset and recovery code are
   absent from the bundle and record its new version; no new merge is needed.
10. Delete only the temporary `CT_RESET_RECOVERY` secret, verify its absence, and
    remove the reset-only Keychain item. Never delete `CT_CURSOR_KEY` or existing
    credentials. Under the owner's subsequent no-compatibility decision, verify
    rejection of the recovery token and the expected incompatibility response for
    the legacy token. Prove reset-code removal using the exact clean source,
    inspected bundle and deployed version. The last authenticated snapshot read
    occurs before clean-code deployment; disclose that no compatible credential
    remains for a fresh read. Do not replace CT_PRINCIPALS to obtain that read.

If a failure occurs after recovery activation, first disable the reset gate, then
revoke recovery by removing `CT_RESET_RECOVERY`, preserving all existing secrets.
Expiry limits residual access but is not a substitute for teardown. Do not roll
back a completed reset to the legacy artifact runtime or rehydrate old data.
If clean-code deployment fails, report that temporary code remains deployed with
its gates disabled; do not substitute unqualified main or claim completion.

## Local qualification

No unit tests or production probes that mutate state were added. The executable
qualification starts actual local Workers with isolated synthetic storage.

- TypeScript: `tsc --noEmit` with contracts generated from the recovery worktree.
- Recovery operations config: dry version upload with synthetic secrets supplied
  through `/dev/stdin`; binding names include ARTIFACTS and WORKSPACES, no secret
  values echoed. No version was uploaded.
- `uv run python scripts/qualify-reset-recovery.py`: original reviewed reset
  qualification, wrong-object RPC guard, four-method allowlist, correct owner
  identity, no existing-reader elevation, invalid/expired configuration, primary
  registry failure, missing reset gate, removed recovery binding, reset snapshot
  zero and other-workspace preservation.
- `uv run python scripts/validate-metrics-baselines.py`: all four committed
  baseline scenarios pass; no expected metric values changed.
- `scripts/check-metrics-quality-gate.sh`: run before committing; recovery does
  not change metric-sensitive paths.
- Clean runtime: archived exact `2917baea`, generated its own contracts, passed
  TypeScript and a dry build. An actual local Worker with fresh synthetic storage
  rejected the recovery token (401), rejected reset even with a normal owner and
  gate present (404), and preserved reader status, snapshot zero and empty
  inventory. This does not claim a production migration or deployed teardown.

Reset and clean-code deployment completed under the revised preproduction
decision. The initial attempt and rollback are recorded separately from the
successful second attempt.
Real-data upload and larger publication-limit qualification remain separate jobs.

Wrangler secret preservation and version-upload behavior follow the
[Cloudflare secrets documentation](https://developers.cloudflare.com/workers/configuration/secrets/#upload-secrets-alongside-code)
and were checked against the installed Wrangler 4.129.1 help and implementation.
