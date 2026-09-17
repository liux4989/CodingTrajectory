# Artifact replacement rollout, 2026-09-17

The reviewed replacement runtime was deployed, but the approved workspace reset
and import did not run. The one permitted quota preflight returned HTTP 503
`database_read_quota_exceeded`. Execution stopped without polling, disabled both
replacement gates, removed the temporary owner grant, and left the frozen export
private on the Mac.

## Frozen source and export

| Item | Value |
| --- | --- |
| Integrated evidence commit | `19f1a366a817171b05c867f9d840954cc3b79104` |
| Runtime commit | `eb290faf86ff06b186c3afec4f5f9f9a3e9eacc6` |
| Base | `f9a55eba1751b5f3afde0b776e99a8f887bdf704` |
| Dry-run `index.js` SHA-256 | `c14d9829d2a939c8e92dc60db8cffc1059677a4115863890934579efabfaf5ac` |
| Replacement SHA-256 | `34fb957816126e744aaf906e95ebaf17eb7f178904da9c9f5d038ced05243ebd` |
| Replacement manifest file SHA-256 | `b7c00fcfa91f2487f8436f8d438440c5d412ed4d1df3927cac3fbd3d7d977475` |
| Workspace | `fbeca960-ce47-4126-ad21-cca95e1855ae` |
| Frozen inventory | 17 sources, 17 graphs, 13,021 facts, 34 objects, 8,499,472 bytes |

Origin `main` had no competing commit and was fast-forwarded to the reviewed
evidence head without rewriting history. TypeScript/schema generation, targeted
Ruff lint and formatting, all metric baselines, the metrics quality gate, and
whitespace checks passed. The dry build contained the existing `WORKSPACES` and
`ARTIFACTS` bindings and Worker-version metadata. The frozen replacement passed
offline validation and preview again without regeneration.

## Credential and deployment sequence

Wrangler authenticated to the expected Cloudflare account with Workers write
authority. The Keychain revision `internal-pilot-2026-09-17-v1` was available as
the active authoritative registry. It contained exactly one `read` and one
`collect` grant, both matching the current production profiles and target
workspace. An encrypted exact backup was read-back verified before modification.

A cryptographically random temporary bearer was stored only in Keychain. Its
registry entry had only `owner`, a new agent UUID, and the approved workspace.
The complete three-entry registry was activated without changing the reader,
collector, cursor secret, namespace, bucket, or other bindings. Owner, reader,
and collector connection checks returned the expected exact roles and version.
No credential value or digest was written to Git, a command argument, or logs.

| Stage | Worker version | Result |
| --- | --- | --- |
| Reviewed runtime, gates absent | `771da047-1b38-4fad-8be0-bbfd26c2bf38` | deployed 100% |
| Temporary owner, gates absent | `6a578f2c-cdb0-451e-8c55-b8e500a2a2a5` | deployed 100%; three roles verified |
| Exact workspace/hash gates | `2bcce1b0-eb6a-4b99-a844-435f93551f3e` | deployed 100%; preflight only |
| Gates removed after quota stop | `af36d23d-641b-4d86-8db1-40e885973c14` | deployed 100% |
| Original two-grant registry restored | `5b1900f9-c9c0-4dfc-bb32-54e23f7b00b7` | final active version, 100% |

The final version contains only `CT_PRINCIPALS` and `CT_CURSOR_KEY` secrets,
`WORKSPACES`, `ARTIFACTS`, and Worker-version metadata. It has neither
`CT_REPLACEMENT_WORKSPACE_ID` nor `CT_REPLACEMENT_EXPORT_SHA256`. The exact
original two-entry registry was restored; reader and collector checks still
return `read` and `collect`, and the temporary owner returns HTTP 401. The owner
bearer and augmented-registry Keychain items were deleted with absence verified.
The encrypted original-registry backup remains for recovery evidence.

## Quota stop and data state

Collection was quiescent before the preflight: the launch job was disabled and
no collector, import, reset, matching cron, or publication process was running.
With the exact gates active, `reset-preview` made its required first
`ct_workspace_snapshot` request. That request returned HTTP 503
`database_read_quota_exceeded`; therefore no reset preview was returned.

No reset request, SQL deletion, R2 deletion, import, artifact upload, publication,
verification read, or retry followed. There is no reset or import receipt. R2
metadata remained 87 objects / 3.09 MB before and after execution. The namespace,
class, bucket, cursor secret, existing grants, schedules, and unrelated workspace
state were not changed. The private export remains frozen locally for a future
separately authorized attempt after quota recovery.
