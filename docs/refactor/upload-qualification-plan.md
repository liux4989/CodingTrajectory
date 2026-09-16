# Internal collection and read rollout

Status: redesign accepted on 2026-09-17 for an early internal pilot. This replaces
the earlier five-gate rollout with three jobs. The two application capabilities
are **collect** and **read**; local and remote describe where their clients run.

Success means one identified collector publishes a bounded set of facts, readers
retrieve the expected result, and retries neither duplicate nor partially expose
a publication. Maximum-capacity qualification is deferred until needed.

## Current starting point

- The retained Worker is `coding-trajectory-control-plane`. Its recorded source
  is `2917baea8b980b40c733e155377ab498f5fa9bd9`, deployed as version
  `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`; this is not current `main`.
- The existing `production-reader` authenticates successfully. Snapshot zero and
  empty inventory were verified after the final clean deployment and again after
  [legacy Datahub retirement](../datahub-retirement-2026-09-17.md).
- A usable current-schema collector grant is still needed. The encrypted
  historical registry is recovery evidence, not a proven copy of the current
  Worker-wide registry. See the [reader recovery record](../credential-recovery-2026-09-17.md).
- Both legacy Datahub Workers and their dedicated Access applications are gone.
  Loop currently runs locally. A hosted Loop deployment is not part of this pilot;
  remote reads must be checked through an actual supported Core consumer.

## Job 1 — align credentials, build and workload

Use the [internal credential design](credential-registry-proposal.md): retain the
working reader and prepare a `collect` grant for the intended collector in one
coordinated credential cutover. Resolve the disposition of old grants before
replacing the complete registry value.

Choose an exact source commit with matching Worker, Python collector and contracts.
Review the full deployed-baseline-to-candidate diff, concentrating on permissions,
publication/read behavior and stored-state migration. Run the existing relevant
build and integration qualification at that source; do not add unit tests for
this operational rollout or treat a local pass as deployed evidence.

Measure the proposed export locally: graph count, row count, encoded bytes and
largest graph. Start with one collector and one publication in flight, and record
explicit pilot bounds. The clean baseline allows an 8 MiB graph and 16 MiB
publication; current `main` raises these to 16 MiB and 96 MiB. These are code
ceilings, not measured capacity guarantees. A small pilot does not need a
maximum-size load test. If its workload needs a newer build, test a representative
bounded workload on that exact build before real upload. Do not split a logical
complete-source publication into semantically different pieces to fit a limit.

Prepare one concrete execution scope covering credential changes, exact build,
target, workload bounds and failure handling. That scope can cover routine
activation and checks together; no approval is needed for each probe. Acceptance
of this redesign does not itself select or deploy a new Core build.

## Job 2 — prove a small deployed collection and read flow

Use a synthetic source with predetermined expected facts. A disposable staging
Worker and separate namespace are appropriate for a risky migration or destructive
failure injection. A permanent staging environment is not required. A reviewed,
explicitly scoped fixture may use the internal target when its state and the
proposed writes make that appropriate.

| Check | Minimum evidence |
| --- | --- |
| Identity and permissions | Expected version, workspace and agent; collector can publish; reader cannot write; collector cannot read without `read`; wrong workspace/agent denied |
| Publication and read-back | Registration, bounded staging, atomic publish and receipt; pinned reads match the fixture's predetermined facts |
| Retry | Repeat the identical completed submission; recover the same receipt without an extra publication or snapshot advance |
| Failure | An incomplete or invalid fixture is rejected without exposing a partial publication |
| Consumers | The Mac reader and intended remote Core consumer retrieve the same published facts at the same snapshot |

Specify fixture identities and all proposed writes before execution. Permission
and failure probes must be bounded to that fixture; use isolated staging if a
fault could otherwise affect existing data. Do not reset the workspace or add a
recovery endpoint. Check the real deployed endpoint, not just a local emulator.

Keep a short evidence record: source/version, workload, expected versus observed
results, receipt, request latency and errors. Stop on wrong identity, unexpected
writes, partial visibility, mismatched facts, duplicate publication or failed
requests; resolve the cause before expanding the run. Full CPU, memory and storage
SLOs are not prerequisites for this small pilot.

If the selected build changes storage, record which committed and staged data
survive, whether pending batches must be restaged, and whether returning to the
old build is compatible. Choose rollback or a forward fix accordingly. Code
rollback does not roll back data; a reset is not a rollback strategy.

## Job 3 — inspect and upload one bounded real export

Inspect the export locally against the shareable fact contract. Raw provider
records, prompts, transcripts, tool bodies, credentials, host paths and private
evaluation evidence remain local. Internal use does not change this boundary.

The upload scope identifies one source, its agent and time window, graph/row/byte
limits, expected read-back and stop conditions. Routine collection uses the recent
seven days plus required graph closure; use a smaller pilot when sufficient.
Do not broaden scope automatically after an error. Authorize the concrete export
before sending real data.

Run one manual publication, retain its receipt, then verify the expected facts
through both intended readers at a pinned snapshot. On an uncertain response,
recover or retry the same immutable submission; do not invent a new identity to
bypass uncertainty. Keep collection schedules paused until the manual flow works
and unattended collection is explicitly enabled. Configuration, checks and reads
must never initiate collection.

## Deferred until the workload requires it

- Full 16 MiB graph / 96 MiB publication boundary and sustained-load qualification.
- Detailed CPU, isolate-memory, transaction and SQLite-growth measurements with
  resource thresholds and service SLOs.
- Broad restart/failure matrices, concurrent multi-host operation and outage catch-up.
- Permanent staging, split-traffic promotion and elaborate release ceremonies.

Deferral preserves enforced limits, authorization, atomicity and immutable retry
semantics. Revisit scale work when size, concurrency, reliance on recovery or
external users grow. A successful bounded pilot does not qualify the maximum
limits or establish general production readiness.
