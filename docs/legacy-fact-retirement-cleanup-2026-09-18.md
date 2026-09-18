# Legacy fact retirement and cleanup rollout, 2026-09-18

The reviewed legacy SQL fact retirement and the authorized empty-table cleanup
completed for workspace `fbeca960-ce47-4126-ad21-cca95e1855ae`. The temporary
cleanup gate was removed immediately after the migration. Final verification
found the six allowlisted tables absent, snapshot 36 unchanged, and artifact
reads unchanged.

## Immutable deployment

| Item | Value |
| --- | --- |
| Source commit | `bde07ed97a2cf7f1e5b2e5710a776ec55934013f` |
| Source tree | `ffde1f42a280175315afa55d667248b2e52a7854` |
| Dry-run `index.js` SHA-256 | `c6d5f4159fe4e28376173b382c1a4620c6d09dbe7486b60ccb79d29f774f2a88` |
| Dry-run bundle | 915.21 KiB; 91.23 KiB gzip |
| Previous version | `6a20f5e0-f6b5-425c-93d3-06869e3c3f5e` |
| Retirement version | `082dba9f-af70-425d-8372-e3088e2e6581` |
| Temporary gate-on version | `198ff13f-e289-4d81-b077-b241b3dfc95f` |
| Final gate-off version | `b871481f-d9e0-4f97-b945-535f5125e871` |
| Deployment interval UTC | `2026-09-18T06:50:32Z`–`2026-09-18T07:05:52Z` |

Control-plane schema generation and TypeScript checking passed with no tracked
change. The actual-workerd cleanup qualification passed 22 checks, including
the six-table allowlist, empty/schema guards, restart behavior, second-workspace
isolation, and R2 preservation. The dry run and deployed version retained only
`WORKSPACES`, `ARTIFACTS`, `WORKER_VERSION`, `CT_PRINCIPALS`, and
`CT_CURSOR_KEY`. Cleanup and replacement gates are absent. The collector launch
job remains disabled, with no matching cron or collector process.

## Preflight and corrected manifest comparison

Three reader RPCs completed without quota, authentication, transport, or
snapshot errors:

- `ct_legacy_fact_cleanup_status` returned snapshot 36, exactly the six
  allowlisted tables, zero rows in all five legacy data tables, one
  `fact_schema` row, and zero legacy publication records;
- the pinned workspace snapshot remained 36;
- the artifact manifest returned exactly one manifest.

The harness then failed its graph-reference tuple comparison before recording
reference parity. This initial assertion is retained in the sanitized execution
record. The approved single diagnostic read parsed both sides through
`ArtifactManifest` and `ArtifactManifestGraph`. It proved exact graph-ID,
digest, count, vendor, facts-object and summary-object reference parity. All 17
timestamp instants were equal and timezone-aware; only their raw UTC spelling
differed (`Z` remotely versus `+00:00` locally). The complete typed comparison
also confirmed workspace, publisher, publication sequence, schemas, inventory,
17 graphs, 34 objects, and 8,499,472 bytes.

## Cleanup and final verification

The same immutable source was deployed with
`CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID` set to the exact authorized workspace.
One status call initialized the target Durable Object and transactionally
dropped only:

- `fact_rows`;
- `fact_schema`;
- `staged_fact_rows`;
- `staged_fact_items`;
- `staged_fact_generations`;
- `validated_fact_graphs`.

The gate was then removed by a same-source deployment, including on the live
100% deployment. Post-cleanup reads confirmed `tables={}`, zero legacy
publication records, snapshot 36, one ID-keyed project, 16 visible cards from
17 manifest graphs, exact prepared-card and typed manifest parity, and canonical
parity for representative `graph.overview` and `session.items` reads against
the frozen facts object. Reader and collector credentials retained exactly
their `read` and `collect` roles. The collector remained disabled.

The bounded rollout used 12 production read RPCs: five through preflight,
diagnostic comparison, and cleanup trigger, followed by seven post-cleanup
status, snapshot, inventory, manifest, one artifact hydration, and two role
checks. No quota, authentication, transport, snapshot, or contract error
occurred. No R2 object, shared metadata, grant, artifact publication/import,
replacement state, schedule, or collector state changed, and no pre-retirement
rollback was performed.
