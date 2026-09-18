# Artifact replacement rollout, 2026-09-17–18

The approved workspace replacement completed after quota recovery on 2026-09-18.
The first attempt below remains the historical quota stop: it made no data
changes. The resumed execution reset only the approved workspace, imported only
the frozen replacement, verified its exact manifest and selected graph objects,
then removed both gates and the temporary owner grant.

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
state were not changed. At that stop, the private export remained frozen locally
pending the separately resumed execution below.

## Resumed completion — 2026-09-18

The Mac resumed with local `main` at the prior rollout record and a newer
`origin/main` containing benchmark and Python projection optimizations. Those
newer sources were not deployed. Production execution used a new clean detached
worktree pinned to evidence `19f1a366a817171b05c867f9d840954cc3b79104`
and runtime `eb290faf86ff06b186c3afec4f5f9f9a3e9eacc6`. The frozen manifest file
and replacement digest, original seven-day window, 17 graphs, 34 objects, and
8,499,472 bytes revalidated without regeneration.

The deployed starting version was the prior safe final version
`5b1900f9-c9c0-4dfc-bb32-54e23f7b00b7`, with both replacement gates absent and
only the original reader/collector registry. The collector launch job remained
disabled; no collector, replacement, publication process, matching cron, upload
claim, or staged row/item was active. One reader snapshot preflight passed before
temporary authority or gates were installed.

The exact gated preview then returned snapshot 63 and `truncated: false`. Its
target R2 prefix contained zero objects. SQL scope contained the prior 29,681
fact rows, 29 staged generations, 245 records, and the expected source,
checkpoint, publication, publisher, and receipt records. It reported zero
artifact upload claims and zero staged fact rows/items. The preserved list and
workspace/export identities matched the approval.

One reset execution returned:

```text
already_complete=false complete=true sql_reset=true deleted=0
```

Thus the old target-workspace SQL data was deleted. No R2 object was deleted
because the exact replacement prefix was empty. Both gates were removed
immediately. A gate-off execute probe returned HTTP 503
`workspace_replacement_unavailable`; the target then reported snapshot 0 and
empty project inventory before import.

The first import attempt used the normal collector credential and was rejected
at its required snapshot preflight with HTTP 403 `capability_required`; it wrote
nothing. The temporary owner was then aligned to the frozen collector agent
identity, retaining only `owner` and the same approved workspace. The frozen
import completed and verified at snapshot 36:

| Result | Value |
| --- | ---: |
| Projects | 1 |
| Graphs | 17 |
| Objects | 34 |
| Object bytes | 8,499,472 |
| Publication sequence | 0 |
| Selected graph read | facts and prepared summary verified |

A separate minimum `verify` repeated those exact counts and selected-object
reads. After restoring the authoritative two-entry registry, the existing reader
and collector returned exactly `read` and `collect`; the temporary owner returned
HTTP 401. A final reader verification again returned snapshot 36 and the exact
replacement counts. The temporary bearer and augmented-registry Keychain items
were deleted with absence verified; the encrypted original-registry backup was
retained.

| Resumed stage | Worker version | Result |
| --- | --- | --- |
| Temporary owner, gates absent | `1e8c57a8-a038-4bfb-abca-1a5d828a5cd6` | owner/read/collect identity verified |
| Exact gates | `76be7bce-381e-4a27-9af8-c8f6ea47415c` | nontruncated preview and completed reset |
| Gates removed | `9af6f696-f481-4f09-ba84-494eb795224c` | reset unavailable; snapshot 0 |
| Owner aligned for import | `807cab31-7d12-404a-adfd-8dcff4670428` | frozen import and verification completed |
| Original registry restored | `df25862e-d304-4429-bc3f-3eec46d5f8f7` | final active version, 100% |

The final version has no replacement gate variables. It retains `WORKSPACES`,
`ARTIFACTS`, Worker-version metadata, `CT_PRINCIPALS`, and `CT_CURSOR_KEY`.
Collection remains manual and disabled; no schedule, paid upgrade, wider source,
other workspace, or other R2 prefix was changed.
