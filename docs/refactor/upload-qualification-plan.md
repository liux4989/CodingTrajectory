# Upload qualification plan

Status: design only, 2026-09-16. This checklist authorizes no staging resource
creation, secret write, deployment, real-data upload or merge. Credential setup
is covered by the [registry proposal](credential-registry-proposal.md).

## Pin separate runtime candidates

| Lane | Exact source | Role |
| --- | --- | --- |
| Reset target baseline | `2917baea8b980b40c733e155377ab498f5fa9bd9` | Last verified clean runtime, version `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`; recheck before use |
| Larger publication candidate | `145ec3d34be32b0a5ed37cad9369af5fd4612146` | Qualification input only; not cleared for deployment to the reset target |

Pin the Python collector/contracts and Worker to the same lane. Generate ingress
validators from that checkout. Record lockfile and generated-bundle digests,
source SHA, environment, Worker version, namespace and credential-role checks.
Do not use whichever `main` happens to contain or build from the recovery branch,
which retains historical temporary code. A changed source needs a new lane and
fresh affected evidence, not a renamed result from an earlier run.

Source-derived budgets, not measured service-capacity guarantees:

| Boundary | Clean baseline | Larger candidate |
| --- | --- | --- |
| HTTP request body | 16 MiB | 3 MiB |
| Canonical staged rows per batch | 512 rows / 2 MiB | Unchanged |
| Canonical individual row | 512 KiB | Unchanged |
| Fact set per graph | 8 MiB | 16 MiB |
| Complete-source publication | 512 graphs / 16 MiB | 512 graphs / 96 MiB |
| Read page | 2,048 rows / 1 MiB | Unchanged |
| Protocol batch-count ceiling | 257 | 265 |

Bytes mean each implementation's canonical encoded representation, not a local
file size or compressed transfer size. Request-envelope, staged-row, graph,
digest-basis and publication bounds are distinct. A graph-sized request is not
the staging protocol. The candidate also caps its digest basis at 16 MiB.

These values come from each pinned source's `shared.ts`, `facts.ts`,
`fact_protocol.py` and `published_facts.py`. The batch-count formula changes from
`131072 / 512 + 1` to that count plus `16 MiB / 2 MiB`.

## Gate A — credential bootstrap and target state

- [ ] Complete the separately approved registry procedure and verify reader
  authentication against the expected clean deployed version.
- [ ] Obtain a fresh snapshot-zero and empty-inventory check on the reset target.
  If either fails, stop; do not repeat the completed reset automatically.
- [ ] Keep all collector schedules paused. A status/check/read command must send
  no registration, staging, publication or heartbeat requests.
- [ ] Select an explicit staging Worker, separate namespace, synthetic workspace
  and least-privilege synthetic credentials. Record their identities before
  deployment. Never point a synthetic qualification script at the reset target.

No staging identity is assumed or created by this design. Resource creation and
staging deployment need their own scoped authorization.

## Gate B — synthetic correctness and failure qualification

Run the baseline and larger candidate in separate isolated staging state. Use
actual local/deployed Worker integration qualification, not new unit tests.
Start from the existing scripts at the chosen source:
`uv run python scripts/qualify-cloudflare-control-plane.py` and, for the larger
candidate, `uv run python scripts/qualify-fact-staging-migration.py`.
Inspect their endpoint and fixture behavior before running; local scripts are
not automatically safe remote load runners. Build any required remote driver as
a separately reviewed, identity-pinned synthetic harness.

| Scenario | Required evidence |
| --- | --- |
| Permissions and isolation | Reader writes denied; collector reads denied; wrong workspace/agent denied; denied operations expose no new visible data |
| Small end-to-end publication | Registration, fenced source, staged rows, atomic publish, receipt, snapshot advance, and complete read-back match a predetermined synthetic manifest |
| Incomplete/corrupt staging | Missing batch, wrong row hash, wrong graph digest, conflicting batch count and malformed manifest rejected without partial publication |
| Boundary sizes | At-limit and one-over cases for bytes, rows, graph count and batch count; account for the outer JSON envelope separately |
| Publication atomicity | A late invalid graph exposes none of the earlier graphs; replacement/tombstone effects share one visibility sequence |
| Staging race | Mutation after validation invalidates the attestation/generation; stale validated rows cannot be published |
| Lost response and duplicate submission | Drop a synthetic publish response, then recover/retry the exact immutable request; receive the original receipt with no duplicate visibility advance |
| Idempotency conflict | Same key with changed request fails; no overwrite or extra visible rows |
| Restart and resume | Restart client/runtime at staging and publication boundaries; rediscover missing batches and recover a committed receipt without rereading private source data |
| Source fencing | Stale epoch/writer and delayed replay cannot advance or replace the current source |
| Read consistency | Traverse every bounded page at a pinned snapshot; no missing/duplicate facts; reject altered, wrong-scope or otherwise invalid cursors |
| Candidate staging migration | Old invisible batches are discarded/restageable, committed facts survive, migration resumes safely after interruption and does not repeat destructively |

Expected counts, hashes, sequences and outcomes must be derived from the
synthetic fixture specification before execution, never copied from observed
output to make qualification pass. Preserve local source evidence for intentional
metric changes. Run the metrics quality gate and the full committed baseline
workflow when changing metric-sensitive code; do not update metric expectations
from command output alone.

## Gate C — deployed runtime capacity

- [ ] Qualify the candidate's exact 16 MiB graph and 96 MiB publication boundaries,
  plus rejection above them, through bounded staged requests on staging only.
- [ ] Measure request latency, CPU, observable isolate memory, errors, retries,
  transaction behavior and SQLite growth. Record telemetry availability explicitly.
  Local process RSS is not isolate heap evidence; a dry build is not runtime proof.
- [ ] Fix a workload envelope before the run: begin with one collector and one
  in-flight publication. Record batch counts, row-size distribution, graph counts,
  concurrency, run duration and repetitions. Do not infer sustained-load capacity
  from one boundary success.
- [ ] Before remote load, record the account's applicable runtime limits and
  concrete latency/CPU/memory/storage stop thresholds. They are deliberately
  unresolved here; no fabricated service SLO or generic platform number is a gate.
- [ ] Stop on partial visibility, cross-workspace effects, inconsistent receipts,
  unexplained sequence changes, unexpected writes, or resource-threshold breach.
  Unavailable required telemetry means unqualified, not presumed pass.

The prior candidate document reports local synthetic results; those are useful
inputs, not fresh results of this plan or deployed capacity evidence.

## Gate D — choose the runtime and preserve data boundaries

Approve an exact deployment SHA only after its relevant gates pass. Remaining
baseline limits must remain visible if the larger candidate is not selected.
Do not silently split a logical complete-source publication into semantically
different smaller publications to fit the baseline.

Before promotion, compare the entire baseline-to-candidate runtime, collector,
contract and migration diff. Preserve existing resources and credentials. Record
an explicit rollback/roll-forward choice before any new-schema write. The
candidate migrates staging: returning to old code after publication requires
qualification, not an assumption. Never roll back application data by resetting
the workspace or replaying arbitrary old artifacts.

Re-probe the deployed source/version and positive reader/collector authentication.
Do not claim readiness solely because code deployment and secret provisioning
succeeded. Keep schedules paused until manual publication is qualified.

## Gate E — separately authorized real-data canary

Only after A–D pass, prepare a concrete canary approval record with: exact
collector/Worker SHAs and deployed version; one selected source and its agent;
allowed fact fields; maximum graph/row/byte counts; one publication in flight;
expected aggregate read-back; and stop/recovery conditions.

Prepare and inspect the export locally first. Transcripts, prompts, raw tool
bodies, credentials, host paths and private evaluation evidence must not leave
the host. Remote output is restricted to the reviewed shareable fact contract;
do not infer privacy from a filename or schema label. Keep fixture and source
manifests private; report only bounded aggregate evidence.

No real-data upload occurs until that prepared canary is explicitly authorized.
Pause on unexpected output or receipt uncertainty; inspect actual state before
retrying. A successful canary qualifies only its stated workload, not automatic
collection, all historical sources or sustained operation.

## Evidence and exit criteria

For each gate record: source/build identity, environment/version, expected versus
observed aggregate results, request outcome/receipt evidence, timestamps,
telemetry limits, stop conditions and disposition. Store no bearer tokens,
registry values, raw sessions or private payloads in committed reports.

Use separate outcomes: **credentials provisioned**, **fresh empty state verified**,
**synthetic protocol qualified**, **deployed workload qualified**, and **canary
authorized/completed**. No label implies the next. This design itself satisfies
none of these execution gates.

Decisions before execution: registry authority/scope; staging resource identities;
which exact runtime lane to qualify; and numerical resource/SLO thresholds for
the proposed workload. The reader-first bootstrap can be considered independently
of larger-limit deployment or real-data upload.
