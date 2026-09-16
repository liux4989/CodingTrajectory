# Internal collection and read credentials

Status: redesign accepted on 2026-09-17. Keep the existing bearer protocol and
principal registry, with two application capabilities and one coordinated setup.
This document changes the operating plan; no registry replacement or credential
issuance has been executed by accepting it.

## Two capabilities

| Credential | Server role | Scope |
| --- | --- | --- |
| Collector | `collect` | One workspace and stable collector agent; registration, staging, publication, recovery and heartbeat |
| Reader | `read` | One workspace; snapshot, inventory, published facts and remote living reads |

Cloudflare account authentication administers the Worker and secrets. This pilot
needs no application owner token, estimator credential, enrollment API or new
credential-management service. The runtime still accepts `owner`; this redesign
does not remove that implementation or claim existing grants have been revoked.

Start with one active collector credential and the working reader credential.
Give another collector its own token and stable agent only when that installation
is enabled. Local and remote are client locations, not additional roles. Keep
bearers in macOS Keychain or the headless host's secret store, never browser
bundles, command arguments, Git or logs. Grant readers only where needed; a
reader can read the workspace's published facts, not just inventory metadata.

## Known clients and recovery evidence

- `production-reader` is working. Its token was originally stored for the legacy
  Datahub reader and is now stored in the Mac profile's Keychain entry.
- `default` and `cloudflare` refer to an old collector credential rejected by the
  current runtime. A valid `collect` grant is still needed. Preserve the intended
  agent and pending delivery state; do not translate an obsolete role silently.
- The old estimator credential is not needed for this pilot.
- The two legacy Datahub Workers and dedicated Access applications were
  [deleted](../datahub-retirement-2026-09-17.md). This removes their deployment
  configuration, not the reader grant still used by `production-reader`.
- Loop currently runs locally. Remote-read acceptance uses the intended Core
  consumer rather than retaining a retired Datahub client.

The [recovery record](../credential-recovery-2026-09-17.md) describes an encrypted
historical reconstruction with three entries. It is not a verified export of the
current registry and cannot prove that no other grants exist.

## One coordinated credential cutover

Prepare a short record of the workspace, known clients, grants to retain/create/
retire and exact target version. The intended minimal result is the existing
reader plus one current `collect` credential, retiring the obsolete collector and
estimator grants as explicitly scoped. Keep collector agent identity and immutable
pending submissions stable across the change.

`CT_PRINCIPALS` is one complete Worker-wide secret value. If a verified complete
current copy is available, update that copy while preserving unrelated grants.
Otherwise, a whole-registry cutover must explicitly account for retirement of old
grants and the affected client/workspace scope. Use a bounded client inventory and
one concrete cutover decision; do not keep searching indefinitely or treat
internal-only use as permission to discard unknown grants. The historical copy
alone cannot establish preservation.

Construct and validate the intended registry privately with Pydantic: digest keys,
canonical workspace/agent UUIDs, supported nonempty roles and expected fields.
Readers also require an agent UUID in registry entries. Store the complete
intended revision encrypted and verify secret read-back in memory. Commit only a
non-secret revision identifier, grant counts and sanitized change evidence.

A reviewed cutover scope covers secure credential storage, activation, profile
updates and verification together. Preserve the existing code unless a matching
build has been selected, and preserve `CT_CURSOR_KEY`, WORKSPACES and ARTIFACTS.
Inspect the resulting version and recheck live identity before activation. Stop
on unexpected drift. Do not add export/reset endpoints to recover a hidden secret.

## Minimal acceptance and later rotation

Verify actual server roles, workspace, agent and deployed version. Client profile
role labels do not grant authority. The [three-job rollout](upload-qualification-plan.md)
then proves one bounded publication, read-back, retry and permission rejection.
Configuration and read commands must produce no collection writes.

For rotation, prepare the registry and client change together, verify the new
token, then retire the old grant with an explicit bounded overlap if needed.
Preserve collector identity and pending work. Removing a local secret or deleting
a client deployment does not revoke its server grant. A failed change can restore
an inspected prior configuration only when doing so does not unintentionally
reactivate a retired grant; configuration rollback does not revert data.

Ordinary bearer expiry is not enforced by the current runtime. Defer automated
rotation, expiry enforcement and a broader lifecycle failure matrix until needed;
do not represent a reminder as a token TTL. The initial acceptance is practical:
the intended collector publishes and the intended readers retrieve that result
with only their required capabilities.
