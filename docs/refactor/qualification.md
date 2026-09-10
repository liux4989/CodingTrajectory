# Acceptance and qualification

The first private deployment is recorded in [operations](operations.md).
Recovery, large-graph, retention, and sustained multi-host work is explicitly
deferred to the [later checklist](later-qualification-checklist.md); it does not
block that first deployment. The full acceptance scenarios below remain the
target for subsequent capability and scale claims.

All new checks are integration/real-runtime qualification, not new unit tests.
Use synthetic or committed sanitized fixtures. Reports contain counts, hashes,
latencies, and bounded failure codes, never raw source bodies or secrets.

## Required scenarios

| ID | Scenario | Passing result |
| --- | --- | --- |
| Q01 | Local query with network unavailable | Correct local result; zero publication requests |
| Q02 | Append, correction, insertion, rotation, truncation, parser upgrade | Canonical parity with full reconstruction; source epoch/offset rules preserved |
| Q03 | Initial large fixture and small append | All operations within advertised budgets; unchanged semantic chunks reused |
| Q04 | Early insertion and long active turn | Bounded stable chunk reuse; no accidental whole-session rechunk/upload |
| Q05 | Hard process exit at every capture/upload/commit/ACK boundary | No lost canonical effects or duplicate published revisions |
| Q06 | New changes while a batch is being retried | Attempted batch bytes/identity stay fixed; new work retained separately |
| Q07 | Duplicate/out-of-order requests and stale source/owner epoch | Deterministic replay or explicit conflict; no stale overwrite |
| Q08 | Two hosts, shared project, independent sessions | Union of permitted committed sessions; one host cannot remove another's history |
| Q09 | Copied source with same IDs; explicit ownership transfer | Identical data deduplicates or conflicts safely; transfer fences former writer |
| Q10 | Partial upload, missing node, wrong digest, cycle, invalid schema | No visible partial revision; bounded rejection and retained retryable work |
| Q11 | Unauthorized workspace, staged digest, nonmember chunk | Access denied without object-store keys or data disclosure |
| Q12 | Pinned list/detail reads during concurrent publish | One coherent revision; no mixed pages or cross-source cache contamination |
| Q13 | Expired cursor and pruned history | Explicit reset/expiration; bounded resnapshot, no silently skipped data |
| Q14 | Heartbeat expiry and delayed offline replay | Liveness unknown when appropriate; historical data retained; observation time remains truthful |
| Q15 | Manual service restart and normal page viewing | No unexpected uploads or automatic mode activation |
| Q16 | Local/shared sanitized graph and metric parity | Exact identity/topology/coverage and contractual numeric parity |
| Q17 | Generic catalog exceeds 10,000 records | Bounded indexed pages continue; no whole-collection limit failure |
| Q18 | Slow network, 24-hour offline preparation, reconnect | Retained backlog drains with bounded memory/disk behavior; blocked state is explicit |
| Q19 | Remote restore, local schema rollback, credential rotation | Incarnation reconciliation and compatible delivery recovery; no false ACK or privilege widening |
| Q20 | Retention races with stage, commit, cursor, and rollback references | No referenced payload deleted; abandoned data eventually reclaimed |
| Q21 | New shared publication after website deployment | Datahub sees it via the API without a site rebuild |
| Q22 | Frozen export selected intentionally | Captured revision/capabilities clear; not silently substituted for live service |

## Workload and measurements

Initial qualification envelope: 20 simulated collector hosts, 100 active sessions,
at least 10,001 retained catalog/change records, a bounded offline backlog, and
synthetic bursts of 10 revision commits per second. These are test inputs, not
production capacity claims. Include graphs below, at, and above the legacy 8 MiB
ceiling: before R5 the above-limit case must reject explicitly; after R5 it must
work through bounded manifest-native operations without whole-graph materialization.

Record parser bytes/records visited separately from encoded bytes transferred,
chunk reuse, CPU time, memory high-water mark, SQLite rows visited, request count,
commit latency, query latency, backlog age, and catch-up time. A tiny HTTP request
is not proof of cheap parsing, bounded DAG reconstruction, or an indexed list.

For automatic mode, measure the configured flush delay plus processing/network
latency end to end. Manual mode has no automatic freshness promise. Any numeric
latency/storage target is agreed configuration and measured before rollout; do
not invent a service guarantee from a small fixture run.

## Existing checks to retain

```sh
uv sync --all-packages
uv run python scripts/validate-local-first-source-selection.py
uv run python scripts/qualify-connection-workflows.py
uv run python scripts/qualify-canonical-repository.py
node scripts/qualify-published-catalog.mjs
uv run python scripts/qualify-live-datahub.py
scripts/check-datahub-static.sh
scripts/check-metrics-quality-gate.sh
uv run python scripts/validate-metrics-baselines.py
npm --prefix cloudflare/control-plane run check
npm --prefix packages/plugins/datahub/web run check:worker
```

Run the existing control-plane and snapshot qualification scripts against their
isolated local harnesses as documented. The upload candidate adds
`scripts/qualify-incremental-upload.py`; reconcile its setup and assertions with
Q01-Q22 before treating it as the new qualification entry point. Do not run the
synthetic write harness against the production workspace.

## Release evidence

For each enabled phase record: exact code version, schema/projection versions,
fixture identity, passing and failing scenarios, observed limits, deployment
version if deployed, authenticated read/publish proof, and rollback procedure.
A local workerd pass does not prove live credentials, browser access, multi-host
continuity, or sustained load. Report these separately.

Docs-only changes require link/consistency validation, not runtime tests. Runtime
changes require the relevant scenario subset plus mandatory repository gates.
Once checks pass, repeat only after changes or unresolved failures justify it.


## Initial implementation evidence (2026-09-10)

| Qualification | Observed result |
| --- | --- |
| Connection workflows | 20 CLI checks; metadata-only preparation/status and local canonical reads work without a token; 4 auth-only requests, zero publication requests |
| Canonical repository | 15 checks including hard exits before/after commit, replay, idempotent capture, and pinned reads |
| Incremental upload | 41 checks; 287/295 chunks reused on append; largest observed request 35,650 bytes |
| Published catalog | 7 SQLite scenarios; 10,001 records across 51 pages; filter and revision cursor isolation |
| Native live Datahub | 11 checks against loopback workerd, including synthetic Access JWT acceptance, signed-out denial, response-model validation, and a later publication seen by the same adapter |
| Existing frozen Datahub | 193 workerd calls and 122 asset digest checks pass |
| Existing control plane | 62 synthetic workerd checks pass |
| Metrics | Mandatory gate and direct full baseline workflow pass all four committed fixtures; no expected values changed |

Control-plane and Datahub Worker TypeScript checks pass. Changed Python files pass
Ruff apart from the pre-existing CLI defensive exception warning (`BLE001`),
confirmed in the committed baseline and left unchanged. Documentation links and
whitespace checks pass. No new unit tests were added.

These are local qualification results, not production deployment evidence. Remote
restore, source rotation, and transient-failure injection have incomplete coverage;
large-graph parsing/validation, garbage collection, and sustained multi-host load
remain release gates. Preserve the existing paused host schedule.

## Fresh local runtime qualification (2026-09-10)

Reran the suite against runtime source revision
`88603db90aaebd0f7a557f6d338a8d44c28fafc2`. The authority used fresh temporary
SQLite/R2 storage, synthetic principals, and loopback workerd. Existing connection
credentials and production storage were not used. The snapshot check read the
existing local export (revision `1789016945819`) through its isolated harness.

| Qualification | Fresh result |
| --- | --- |
| Connection workflows | 20 checks; 4 authentication requests; zero publication requests |
| Canonical repository | 15 checks; 2 hard process-exit boundaries |
| Incremental upload | 41 checks; 287/295 chunks reused; 7 upload requests; largest request 38,756 bytes |
| Published catalog | 7 scenarios; 10,001 records; 51 pages |
| Native live Datahub | 11 checks; 9 native read requests; zero read-triggered publication requests |
| Existing control plane | 62 checks |
| Existing frozen Datahub | 193 workerd calls; 122 asset digest checks |
| Local-first source selection | Passed |
| Datahub static checks and both Worker TypeScript checks | Passed |
| Mandatory metrics gate and direct baseline workflow | Both passed: 4 fixtures, 107 assertions, 49 invariants each |

Every command exited successfully; no runtime repair or expected metric change
was required. The request-size measurement supersedes the earlier run's value
for this run only; it is not a general upper bound. This rerun covers the existing
harness assertions, not all Q01-Q22 acceptance scenarios. Recovery gaps and
large-graph, retention, and sustained multi-host gates above remain open. No
deployment or host schedule activation was performed.
