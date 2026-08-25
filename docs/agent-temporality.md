# Agent Temporality: Forecasts, Actuals, and Calibration

## Status

Proposed design, revision 2. No implementation exists yet.

The mechanism extends the Pydantic service contracts in
`packages/core/src/coding_trajectory/contracts.py` and the `code-time`
surfaces of the `datahub` plugin. It does not introduce a new plugin and does
not make model output canonical CT data.

Phase 1 produces explicitly labeled historical backcasts. Genuine prospective
forecasts require task-intake instrumentation and are introduced in phase 2.
The two populations are never combined in calibration statistics.

## Background

Coding agents are not time-aware. Research on frontier CLI agents
([Your Agents Are Not Time Aware](https://www.lesswrong.com/posts/eAbuPXbjakop5rSJx/your-agents-are-not-time-aware),
MATS 10) shows:

- Prospective estimates are flat priors, with substantial over-prediction that
  is worst on short tasks. More task detail alone does not improve calibration.
- Runtime is harness-determined. The same model can run for materially different
  durations and numbers of turns under different harnesses.
- Retrospective estimates borrow from timestamps and tool-call durations in the
  transcript. Transcript length is also a strong proxy for elapsed runtime.
- Self-scoring is poorly correlated with measured outcomes.

Because agents externalize temporal judgment, the human supplies scheduling,
budgets, stopping decisions, and quality review. CodingTrajectory already
reconstructs the facts needed to measure elapsed work: canonical task requests,
turn and item timestamps, runtime observations, token usage, target agent
identity, and optional versioned evaluation artifacts. This design uses those
facts without treating an after-the-fact model estimate as a prediction.

## Terms and External Reference Mapping

| Reference concept | CodingTrajectory interpretation |
| --- | --- |
| Prospective forecast | A forecast issued after a request is available but before the target harness begins work, using only evidence available at issue time. |
| Historical backcast | A forecast generated after the target execution occurred, under a point-in-time retrieval simulation. Useful for bootstrapping and estimator development, but not evidence of prospective calibration. |
| Runtime advisory | An estimate requested after target execution has started. Useful for future control surfaces, but excluded from prospective calibration. |
| Actual duration | Human-facing wall-clock duration of one canonical turn episode, derived deterministically from its observed timestamps. |
| Harness | The target agent runtime, including name, version, and execution-policy fingerprint. Vendor alone is not a harness identity. |
| Calibration ratio | Geometric mean of forecast median divided by positive actual minutes within one declared cohort. |
| Compression exponent | Log-log slope of forecast median versus actual minutes; 0 is a flat guess and 1 is perfect tracking. |
| Clock access | Future exposure of elapsed and budget facts to a running agent. Out of scope for forecast calibration. |

## Design Invariants

- Forecast kind is assigned by CT from observed timing and evidence, not trusted
  from a caller-provided label.
- A forecast is prospective only when it is issued before the first target-agent
  activity for the turn. A completed or already-running turn can only receive a
  historical backcast or runtime advisory.
- Historical retrieval is point-in-time. A backcast cannot retrieve the target
  turn, its session graph, or any example completed after the target request
  became available.
- Backcasts, prospective forecasts, and runtime advisories remain separate
  populations in storage, APIs, and reports.
- Target harness identity is distinct from estimator identity. Both are recorded.
- Actual duration, outcome, and post-task evaluation are never estimator inputs.
- Duration buckets are outcome diagnostics, not task difficulty and not an
  independent calibration target.
- Model-generated forecast artifacts are durable derived evidence. They are not
  canonical facts, but they cannot be reproduced exactly by rerunning a model.
- Calibration never mixes estimator, prompt, retrieval-policy, target-harness,
  or forecast-kind versions implicitly.
- Missing target configuration or outcome evidence remains `unknown`; CT does
  not infer it from model prose.

## Product Questions

- Before target execution starts, how long is this turn episode likely to take
  on this project, under this harness and configuration?
- How far off are forecasts for comparable cohorts, and does error shrink as
  genuinely prospective history accumulates?
- Which observed harness configurations complete comparable task classes faster
  on this repository?
- Where does error concentrate: task class, actual-duration bucket, target
  harness, target model, project, or outcome?
- Which conclusions come from historical backcasts and which come from live
  prospective forecasts?

## Prediction Unit and Actual Target

Phase 1 and phase 2 predict one **canonical turn episode**: one user request and
the target-agent work caused by that request until the turn reaches a terminal
state. Later user turns receive separate forecasts.

The target is wall-clock elapsed time, not additive compute time:

```text
actual_execution_seconds = turn.ended_at - turn.started_at
```

This interval may include overlapping spawned-agent work, tool latency, and
approval or user waiting if those occur before the turn terminates. Child-agent
durations are not added to the parent duration. Reports must expose interruption
and waiting evidence when available so wall-clock latency is not presented as
pure model compute.

Task extraction does not automatically reject the word "continue." A continuation
that authorizes unresolved work is an eligible turn when its pre-turn context
makes the requested work identifiable. Pure acknowledgements, status checks,
clarification answers that authorize no work, interrupted turns, and turns with
no trustworthy terminal timestamp are `not_applicable` or excluded with a
reason.

This phase does not claim to predict a multi-turn human objective. A future task
lineage may group requirement changes, corrections, and continuations, but its
forecast and actual contract must be specified separately.

## Temporal Eligibility and Forecast Kinds

Each candidate exposes three relevant times:

- `task_available_at`: when the user request became available to the harness.
- `target_execution_started_at`: first observed target-agent activity after the
  request. This is distinct from the request timestamp when the evidence permits.
- `issued_at`: when CT durably records the forecast.

CT assigns one of these kinds:

| Kind | Eligibility | Calibration use |
| --- | --- | --- |
| `prospective` | Bound to a turn with `task_available_at <= issued_at < target_execution_started_at`; no post-start evidence was used. | Primary live calibration population. |
| `prospective_unbound` | Created from task text and a declared target configuration before a turn ID exists. | Excluded until bound once to a compatible later turn. |
| `historical_backcast` | Generated after execution, with a recorded point-in-time data cutoff. | Separate backcast-only development cohort. |
| `runtime_advisory` | Issued after target execution started. | Excluded from forecast calibration. |

An unbound forecast may be bound exactly once. Binding succeeds only when its
`issued_at` precedes the bound turn's first target activity and the task
fingerprint and declared target configuration match. The binding receipt is
immutable.

For one target turn and estimator cohort, the earliest eligible prospective
forecast is the primary calibration observation. Repeated forecasts are retained
as diagnostic trials but cannot increase the primary sample count. Historical
backcasts are idempotent per target turn and estimator cohort.

## Architecture Placement

The mechanism lives in the application service layer. Frontends remain thin
consumers:

```text
packages/core/src/coding_trajectory/contracts.py
  — gains versioned estimate.* Pydantic contracts

packages/core/src/coding_trajectory/estimation/
  task.py         — pre-execution task candidate and eligibility
  retrieval.py    — deterministic point-in-time reference selection
  provider.py     — estimator provider interface
  codex.py        — Codex app-server provider adapter
  ledger.py       — durable forecast and attempt ledger
  comparison.py   — deterministic actual joins and diagnostics
  calibration.py  — deterministic cohort statistics
  jobs.py         — resumable backfill scheduling and budgets

packages/plugins/datahub/code_time.py
  — renders service results; it does not invoke the estimator directly
```

The existing app-server transport currently lives in the datahub plugin. The
shared transport required here must move to an application-owned module before
the estimator is implemented. Core service methods must not import another
plugin, and the code-time surface must not depend on web dashboard internals.

Boundary rules:

- Canonical sessions, turns, items, timestamps, and usage remain the source of
  observed execution facts. No forecast is written into canonical resources.
- Estimator output is non-deterministic derived evidence. CT owns eligibility,
  retrieval, prompt assembly, schema validation, attempt state, comparison, and
  aggregation. Codex app-server owns only the bounded semantic inference turn.
- Optional outcome stratification consumes versioned evaluation artifacts when
  they exist. Forecasting does not depend on an evaluation implementation, and
  missing evaluations remain `unknown`.
- Historical target harness fields are projected only when observed in immutable
  source evidence or canonical extensions. Unsupported fields remain unknown.
  Adding consumer-neutral harness metadata to canonical ingestion is separate
  core contract work; the estimator must not guess it.

## Task Snapshot

A task snapshot contains only information available before target execution:

- User request text for the turn.
- Project identity and a content fingerprint, not an unrestricted checkout.
- Session title and a compact, versioned summary of prior turns.
- Declared target harness, model, effort, and execution-policy fields when known.
- Task class from a pre-execution task contract when available.

The snapshot never includes the turn's own agent items, tool output, completion
state, actual duration, evaluation, or post-task content. Its normalized payload
and fingerprint are stored with the forecast so later extraction changes do not
silently rewrite what the estimator saw.

## Target Execution Configuration

Every forecast records the target configuration separately from the estimator:

- `target_agent_vendor`
- `target_harness_name` and `target_harness_version`
- `target_model` and `target_effort`
- `target_execution_policy_fingerprint`
- observed approval, sandbox, tool, and multi-agent policy fields when available

Unknown fields are explicit. Harness-comparison reports require a known harness
name and version or a declared stable configuration fingerprint. Vendor-only
records may appear in coarse descriptive reports but cannot support claims about
harness performance.

## Point-in-Time Retrieval

Retrieval is mandatory for the reference-class estimator and is deterministic.
For each forecast, CT:

1. Selects only examples whose terminal evidence was available at or before
   `data_cutoff_at`.
2. Excludes the target turn, every session in its graph, and any record sharing
   the target task fingerprint.
3. Applies a versioned fallback hierarchy over project, target harness, target
   model, task class, and semantic task similarity.
4. Returns at most `k` examples with their observed wall-clock duration, known
   target configuration, and optional versioned outcome.
5. Stores the ordered example IDs, values, retrieval-policy version, corpus
   fingerprint, and cutoff as the retrieval snapshot.

For a historical backcast, `data_cutoff_at` is no later than
`task_available_at`. For a prospective forecast it is no later than `issued_at`.
The same-project preference is a fallback policy, not permission to use future
examples.

## Estimator

The first provider uses Codex app-server: an ephemeral thread, neutral service
working directory, no access to the target checkout, fixed estimator model and
effort, and strict output schema. Tool access is disabled where the protocol
permits; a read-only sandbox alone is insufficient because it would still allow
the estimator to inspect post-task repository state. One stateless turn produces:

```json
{
  "p50_minutes": 12,
  "p80_minutes": 25
}
```

`p50_minutes` is the primary point forecast. `p80_minutes` exposes forecast
uncertainty and supports interval-coverage diagnostics. Both must be positive,
finite, and within declared operational bounds. CT does not ask the estimator
for realized complexity or self-score.

The provider sits behind an interface, but provider substitution creates a new
estimator cohort. It never changes an existing forecast in place.

## Durable Forecast Ledger and Derived Read Models

The forecast ledger is a durable, append-only derived-evidence store. It is not
canonical CT data and does not carry vendor-log compatibility guarantees, but
successful model outputs must not be treated as disposable or exactly
reproducible.

For every attempt, the ledger retains:

- Task snapshot and fingerprint.
- Eligibility decision and forecast kind.
- Target execution configuration.
- Retrieval snapshot and observed example values.
- Exact normalized estimator prompt payload and content fingerprint.
- Estimator provider, model, effort, prompt version, schema version, and relevant
  app-server configuration.
- Exact structured response or terminal failure record.
- Creation, issue, binding, comparison, and attempt timestamps.
- Canonical source and parser fingerprints used for later actual joins.

Calibration tables and UI projections are disposable read models rebuilt from
canonical execution facts plus this durable ledger. If the ledger format changes,
it is migrated or exported; CT does not silently rerun the model and call the new
outputs the same forecasts.

## Forecast Record

One immutable successful record contains at least:

| Field | Source |
| --- | --- |
| `prediction_id`, idempotency key | CT |
| `forecast_kind`, primary/diagnostic role | eligibility policy |
| `turn_id`, binding receipt, task fingerprint | task and binding layer |
| `task_available_at`, `target_execution_started_at`, `issued_at`, `data_cutoff_at` | canonical evidence + CT clock |
| project and pre-execution task class | task snapshot |
| target vendor, harness, version, model, effort, policy fingerprint | declared and observed target configuration |
| `p50_minutes`, `p80_minutes` | estimator |
| ordered retrieval snapshot and evidence turn IDs | CT retrieval |
| estimator provider/model/effort and prompt, schema, retrieval-policy versions | estimator configuration |
| actual execution seconds and actual-duration bucket | deterministic comparison |
| optional evaluation artifact ID, version, and outcome | evaluation layer |
| source/parser fingerprints | provenance |
| `created_at`, `bound_at`, `compared_at` | CT |

Actuals are joined when the bound turn reaches a trustworthy terminal state.
Unbound and uncompared forecasts remain queryable with explicit status.

## Backfill Jobs and Failure Semantics

Full-corpus backfill is a durable job, not a loop hidden inside one API request.
The job layer provides:

- Deterministic candidate inventory and idempotency keys.
- Resumable checkpoints and restart-safe claim/lease handling.
- Attempt states: `pending`, `running`, `succeeded`, `retryable_failed`, and
  `permanent_failed`.
- Bounded retries for transport and provider failures; schema or eligibility
  failures remain inspectable rather than silently skipped.
- Configured concurrency, rate limits, timeout, token/cost budget, and a stop
  receipt when any budget is exhausted.
- Progress counts by eligible, excluded, succeeded, failed, and uncompared.

The existing app-server manager serializes turns on one connection. Backfill
workers may use a bounded pool of independent provider sessions, but scheduling
and budgets remain CT-owned. A process restart resumes from the ledger instead
of regenerating successful forecasts.

## Contract Methods

Versioned Pydantic methods are dispatched through `ServiceRuntime`:

| Method | Behavior |
| --- | --- |
| `estimate.predict` | Pre-execution task snapshot or historical `turn_id` to one forecast record. CT assigns forecast kind. |
| `estimate.bind` | Bind one `prospective_unbound` forecast exactly once to a later compatible turn. |
| `estimate.get` / `estimate.list` | Query forecast, eligibility, attempt, binding, and comparison records. |
| `estimate.calibration` | Compute declared cohort statistics with exclusions and uncertainty. |
| `estimate.backfill.start` / `estimate.backfill.status` | Create or inspect a resumable historical-backcast job. Requires a durable service owner; a foreground CLI wrapper may run the same job protocol. |

`estimate.predict` remains suitable for one synchronous forecast. It does not
perform corpus backfill. Runtime errors are converted into versioned structured
failure responses rather than escaping the service envelope.

## Comparison and Calibration

Actual-duration diagnostics use stable, declared wall-clock bins rather than
corpus-relative complexity labels. Initial bins are configuration, versioned
with the calibration response, and may resemble:

```text
under_5m, 5_to_20m, 20_to_60m, 1_to_3h, over_3h
```

Outcome is a separate dimension such as resolved, partial, failed, unknown, or
not evaluated. A long successful turn and a long failed turn remain in the same
duration bucket.

Every calibration request declares or returns a cohort key containing:

- Forecast kind.
- Estimator provider/model/effort, prompt version, schema version, and retrieval
  policy version.
- Target project, harness/configuration, model/effort, and optional task class.
- Issue-time and actual-completion windows.

Rollups across a dimension are explicit. The default never merges versions.

For each cohort or bucket, the response includes:

- Eligible sample count, primary sample count, and exclusion counts by reason.
- Geometric mean `p50 / actual` for positive actuals.
- Median absolute log error and share within 1.5x of actual.
- Compression exponent only when the cohort has enough positive, non-constant
  observations; otherwise `undefined` with a reason.
- P80 interval coverage: share where actual minutes are at or below `p80`.
- Deterministic uncertainty intervals for aggregate statistics.
- Outcome-stratified diagnostics only when compatible evaluation versions exist.

Zero-duration, missing-terminal-time, interrupted, duplicate, and unbound records
are excluded from log statistics with counted reasons. The minimum sample size,
variance rule, interval method, and deterministic seed are part of the versioned
calibration policy.

## Surfaces

- `ct api call estimate.predict ...` supports a single backcast or intake
  forecast according to temporal eligibility.
- `ct plugin datahub code-time forecast` renders forecast-versus-actual for
  eligible records while visibly labeling backcast versus prospective evidence.
- The datahub Code Time tab exposes aggregate calibration cohorts, sample sizes,
  exclusions, and version filters. It does not elevate one noisy prediction into
  a performance conclusion.
- Future harness integration may request and bind a forecast before starting the
  target agent.
- Future MCP exposure may provide runtime advisories or budget facts to a running
  agent. Those records are not reclassified as prospective forecasts.

## Non-Goals

- Runtime clock or budget tools injected into a live agent loop.
- Watchdog enforcement of time budgets.
- Predicting a multi-turn human objective in phase 1 or phase 2.
- Calling duration or outcome an independent measure of task difficulty.
- Reconstructing lost model outputs by rerunning a nominally identical model.
- Harness-performance claims from vendor-only or unknown-version records.
- Per-turn UI conclusions unsupported by an aggregate cohort.

## Phasing

1. **Historical foundation:** task snapshots, target-configuration projection,
   durable ledger, point-in-time retrieval, resumable backcast jobs, actual joins,
   and backcast-only calibration. Validate that leakage exclusions and custody
   survive restart before producing product claims.
2. **Prospective intake:** `estimate.predict` before target execution,
   `prospective_unbound` binding, primary-observation deduplication, and separate
   prospective calibration. Do not claim improvement until this population has
   sufficient samples.
3. **Operational integration:** durable service ownership, harness-side intake,
   aggregate code-time dashboards, and bounded estimator-provider scaling.
4. **Advisory and control research:** MCP runtime advisories, elapsed/budget facts,
   and duration-control experiments. Keep these populations separate from
   prospective forecast calibration.

## Acceptance Gates

Before implementation is called complete:

- A historical target cannot retrieve itself, its session graph, or future
  examples.
- A forecast created after target-agent activity cannot be labeled prospective.
- Restarting a backfill does not duplicate or regenerate successful forecasts.
- Calibration defaults do not merge forecast kinds or estimator/harness versions.
- Every aggregate reports sample and exclusion counts, and invalid log statistics
  are explicitly undefined.
- Deleting a disposable calibration projection does not delete the durable
  forecast artifacts needed to reconstruct it.
- The code-time surface consumes only versioned service results and has no
  dependency on web dashboard internals.
