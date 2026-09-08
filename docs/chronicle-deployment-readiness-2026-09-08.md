# Chronicle Deployment Readiness — 2026-09-08

- **Result:** Blocked at migration-history reconciliation
- **Mode:** Read-only inspection; no migration, reset, repair, or publication
- **Target evidence:** The linked project was active and healthy; its project
  reference and credentials are intentionally omitted

## Observed state

The remote migration ledger and local repository agreed through migration
`20260905000000`. The remote also recorded `20260905010000`, the superseded Amp
compatibility migration that the Chronicle cleanup removed locally.

The committed Chronicle refactor also changed migration `20260904000000` under
an already-applied version. A migration-list match at that version therefore
does not prove that the remote schema matches the current Chronicle SQL.

The linked dry run stopped before applying SQL and reported that local migration
`20260905010000` was missing. The suggested migration-history repair was not
run. Marking that migration reverted would change only the ledger; it would not
undo schema objects already created by the superseded migration.

## Admission decision

The current repository is locally qualified but is not incrementally deployable
to this existing database as-is. Remote deployment remains inadmissible until
one of these paths is explicitly selected:

1. **Authorized non-production reset.** Reconfirm the exact target and its
   non-production classification, reset the CT application schema, apply the
   complete committed migration chain, and then publish one bounded canary.
2. **Forward-only reconciliation.** Restore the applied historical migrations
   byte-for-byte and add a new migration that transforms the deployed schema to
   Chronicle while preserving existing data and migration history.

For the previously designated disposable non-production CT application, the
reset path is the smaller and cleaner operation. It is destructive and requires
fresh explicit authorization. The forward-only path is required if any retained
remote data or shared environment must be preserved.

## Next verification after authorization

Whichever path is selected must establish:

- the migration ledger and deployed function definitions match committed SQL;
- `ct.chronicle_graph.v1` is accepted and the retired schema is rejected;
- one canary preserves digest, source-vector, and resource-index integrity;
- exact retry is idempotent and stale publication is rejected; and
- a fresh remote-only client passes supported reads while evidence-body methods
  fail closed.
