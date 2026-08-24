# Agent Temporality: Prediction, Actuals, and Calibration

## Status

Proposed design. No implementation yet. The mechanism extends the existing
service layer and the `code_time` surfaces; it does not introduce a new plugin.

## Background

Coding agents are not time-aware. Research on frontier CLI agents
([Your Agents Are Not Time Aware](https://www.lesswrong.com/posts/eAbuPXbjakop5rSJx/your-agents-are-not-time-aware),
MATS 10) shows:

- Prospective estimates are flat priors (~90 minutes regardless of task), with
  3–10x over-prediction, worst on short tasks. More task detail does not
  improve calibration.
- Runtime is harness-determined: one harness runs until self-judged completion,
  another stops around a fixed duration; the same model takes ~2.5x more turns
  in one harness than the other.
- Retrospective estimates are borrowed from timestamps and tool-call durations
  in the transcript; stripping time cues doubles error, while a clock tool
  makes them near-perfect. Transcript token length alone correlates with true
  runtime at r = 0.91.
- Self-scoring is uncorrelated with measured scores.

Because agents externalize all temporal judgment, the human supplies it:
scheduling, budgets, stopping decisions, and quality review. CodingTrajectory
already owns the ground truth the agents lack — canonical turn timestamps,
execution time, token usage, and evaluation scores across vendors and projects.
This design turns that ground truth into a calibration loop.

## External Reference Mapping

| Reference concept | CodingTrajectory interpretation |
| --- | --- |
| Prospective prediction | `estimate.predict` over a task description blind to the outcome. |
| Retrospective estimation | Post-session comparison of a logged prediction against canonical `execution_seconds`. Not produced by re-asking an agent that saw the trajectory. |
| Harness | The agent vendor/runtime recorded on each session; a primary conditioning dimension for base rates. |
| Compression exponent | Log-log slope of predicted vs actual minutes; 0 is a flat guess, 1 is perfect tracking. |
| R-oracle clock access | Future MCP exposure of elapsed/budget facts to a running agent. Out of scope for phase 1. |

Design rules adopted from the research:

- Predictions must be made blind to the trajectory. An estimate produced from
  the completed session measures retrospection, not prediction, and must never
  enter calibration statistics as a prediction.
- Estimates derived from task detail alone reproduce the flat-prior failure
  mode. The estimator must be conditioned on historical base rates (reference
  class forecasting), not on longer task descriptions.
- Harness identity is a primary conditioning dimension, at least as important
  as model identity.
- Error direction depends on task length: short tasks are over-predicted,
  multi-hour tasks approach parity. Calibration reporting must be stratified
  by realized complexity, never only global.
- Wording shifts estimates by ~10%. Single estimates are noise; only aggregate
  statistics are meaningful.

## Product Questions

- How long will this task take on this project, with this harness, before it starts?
- How far off are those predictions, and does the error shrink as history accumulates?
- Which harness completes which task classes faster on this repository?
- Where does prediction error concentrate: task type, complexity bucket, vendor, or project?

## Architecture Placement

The mechanism lives in the backend service layer. Frontends remain thin
consumers. No new plugin is introduced.

```
session-api.json (contract)        — gains versioned estimate.* methods
coding_trajectory/estimation/      — new backend module
  extractor.py   — turn → task candidate
  estimator.py   — one Codex app-server turn per prediction,
                   retrieval-augmented prompt
  compare.py     — prediction vs actual, realized complexity
  store.py       — SQLite derived store
packages/plugins/code_time/        — existing frontend surface, renders
                                     predicted-vs-actual and calibration
```

Boundary rules, consistent with the PRD:

- The canonical layer is untouched. Turn timestamps, user messages, usage, and
  runtime are already canonical facts.
- The estimator is the first non-deterministic backend component, delegated
  to Codex app-server. It only *proposes* `{complexity, minutes}`.
  Comparison, realized complexity, and calibration statistics are
  deterministic CT-owned computations over canonical timestamps and
  evaluation scores.
- The prediction store is a consumer-owned derived artifact. It is rebuildable
  from immutable logs plus recorded estimator versions, and carries no
  canonical compatibility burden. It is global-scope, since calibration
  aggregates across projects.
- Every prediction records estimator model/version and evidence turn IDs, so
  each estimate is auditable against the logs — the same provenance discipline
  as the evaluation layer.

## Task Extraction

A task candidate is one turn's user message plus the minimal context needed to
make it self-contained:

- The user message text of the turn.
- A compact prefix: project name, session title, and a short running summary
  of prior turns. Later turns ("now add tests") are meaningless without it.
- Never the turn's own items, tool output, or any post-task content.

Extraction filters non-tasks: acknowledgements, "continue", status-only
messages, and clarification replies are marked `not_applicable`, matching the
evaluation layer's treatment of non-evaluable turns.

Extraction runs over the full historical corpus, so the calibration loop has
data from first deployment rather than after an instrumentation period.

## Estimator

The estimator backend is Codex app-server directly, reusing the existing
client (`codex_app_server.py`: ephemeral thread, read-only sandbox, fixed
model and effort, strict output schema) — the same pattern the evaluation
layer uses for its semantic judge. One stateless turn produces one
prediction; the original task thread is never resumed for estimation.

CT-owned code stays thin: extraction, retrieval, prompt assembly, schema
validation, and storage. No custom LLM client, retry, or concurrency
machinery is introduced. The backend sits behind an interface so a future
estimator provider does not change the `estimate.*` contract.

Per prediction:

1. Retrieve the k most similar past task candidates (same project and vendor
   preferred, similar task text) **with their actual execution durations and
   realized outcomes**. Retrieval is deterministic and CT-owned; the app
   server receives only the assembled prompt.
2. Run one app-server turn with a strict output schema producing
   `{predicted_complexity, predicted_minutes}` from the task text, prefix
   context, and retrieved base rates.
3. Validate the schema response and emit one prediction record.

The retrieval step is mandatory. Without observed base rates the estimator
inherits the flat-prior failure mode documented in the research, and the
calibration loop cannot distinguish "model prior" from "data-backed estimate".

## Two-Stage Complexity

Complexity exists twice, at different times, from different evidence:

- **Predicted complexity** (prospective): the estimator's `{S, M, L}` judgment
  from task text and base rates. Produced before execution.
- **Realized complexity** (retrospective): a deterministic label computed from
  the turn's actual `execution_seconds` (quantile within the project) combined
  with the evaluation score. Long and successful is genuinely complex; long
  and failed is hard or failed; short is simple.

Realized complexity must never be an estimator input. It is derived from the
prediction target itself; feeding it in is target leakage.

The initial realized formula is intentionally coarse: time tertile within the
project, adjusted down one grade on failure. A continuous complexity function
is a non-goal until aggregate data justifies it.

This yields two independent calibration signals: predicted-vs-actual minutes,
and a predicted-vs-realized complexity confusion matrix that explains *where*
the minute error comes from.

## Prediction Record

One record per prediction:

| Field | Source |
| --- | --- |
| `prediction_id` | CT-assigned |
| `turn_id` / task text fingerprint | extractor |
| `project_name`, `agent_vendor`, model identity | canonical session |
| `predicted_complexity`, `predicted_minutes` | estimator |
| `evidence_turn_ids` (retrieval base rates used) | estimator |
| `estimator_model`, `estimator_version`, prompt version | app-server turn config |
| `actual_execution_seconds` | canonical runtime, joined post-session |
| `realized_complexity`, evaluation score | compare + evaluation layer |
| `created_at`, `compared_at` | CT |

Predictions are logged at creation; actuals are joined when the turn completes.
Uncompared predictions remain queryable.

## Contract Methods

Three versioned methods on the existing service contract, dispatched through
the same `ServiceRuntime` as `project.list` and `graph.usage`:

| Method | Behavior |
| --- | --- |
| `estimate.predict` | Task text or `turn_id` → prediction record. Runs extractor + estimator. |
| `estimate.list` / `estimate.get` | Prediction records with filters (project, vendor, compared/uncompared), actuals joined where available. |
| `estimate.calibration` | Aggregate statistics per project × vendor × realized-complexity bucket. |

`estimate.calibration` reports, per bucket:

- Calibration ratio: geometric mean of predicted / actual.
- Compression exponent: log-log slope of predicted vs actual.
- Share of predictions within 1.5x of actual.
- Predicted-vs-realized complexity confusion matrix.

All statistics are computed deterministically from the prediction store.

## Surfaces

- `code_time` CLI report: predicted-vs-actual column alongside existing
  execution time, cost, and tokens.
- `code_time` web: predicted-vs-actual per session; calibration view per
  project and vendor.
- `ct api call estimate.predict ...` works immediately for scripts.
- Future: MCP exposure of `estimate.predict` so an agent at task start can
  query a data-backed estimate instead of falling back to its prior. This is
  the prospective counterpart of the research's clock-tool fix, and requires
  the service layer to run as a durable server rather than per-invocation
  in-process calls.

## Non-Goals (Phase 1)

- Runtime clock/budget tools injected into a live agent loop.
- Watchdog enforcement of time budgets.
- Continuous complexity scoring functions.
- Per-session UI around single predictions (noise, per the wording finding).

## Phasing

1. Extractor + store + backfill over historical corpus; `estimate.list` and
   `estimate.calibration`; `code_time` renders the first comparison data.
2. `estimate.predict` at task intake via `ct api`; calibration dashboards.
3. MCP exposure and prospective agent-facing queries.
