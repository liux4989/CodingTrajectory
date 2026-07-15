# Coding Session Evaluation: Full System Design

## Status

Phase 2 design. Implementation begins only after the contracts and acceptance gates in [`session-evaluation-foundation-lite.md`](session-evaluation-foundation-lite.md) pass. The product and mechanism are defined in [`session-evaluation-high-level-design.md`](session-evaluation-high-level-design.md).

## Objective

Apply evaluation continuously across daily coding usage, preserve category-appropriate proof, and produce fair model-performance reports for real projects.

The full system extends the lightweight foundation with:

- Production rubric families and evaluator calibration.
- Isolated executable verification and reusable verifier components.
- Delayed verification and optional human signals.
- Historical backfill and continuous evaluation.
- Difficulty calibration and cohort-normalized model reporting.
- Frozen-task replay across models and harnesses.

## Preconditions

Phase 2 does not begin until:

- The evaluation artifact schema is frozen at a named version.
- The lightweight cohort meets category, schema, safety, and evidence gates.
- Turn and session evaluation boundaries are stable.
- Semantic and executable conflicts are represented without silent score blending.
- The executable side-effect policy has no known unsafe automatic path.
- Evaluation cost and latency have been measured on the lightweight cohort.

## Production Architecture

```text
Canonical session graph and project index
  -> evaluation scheduler
  -> source fingerprint and snapshot resolver
  -> eligibility, category, difficulty, and rubric compiler
  -> evidence builder
      -> semantic judge pool through evaluator-agent adapter
      -> executable verifier through isolated runner
      -> delayed observation collector
      -> optional human signal collector
  -> criterion adjudication and evaluation lifecycle
  -> immutable evaluation store
  -> cohort metrics and model-performance projections
  -> selected frozen tasks exported for replay
```

CodingTrajectory owns identities, provenance, evaluation lifecycle, and reporting. Codex app-server remains the first evaluator-agent backend behind an interface so a future Codex SDK or another judge provider does not change the evaluation contract.

## Category Rubric Families

### Repository Engineering

The rubric follows DeepSWE's behavioral-verification pattern without requiring a reference patch.

Default criteria cover:

- Explicit requirement coverage.
- Observable requested behavior.
- Regression preservation.
- Integration with existing code and configuration.
- Scope discipline and prohibited changes.
- Project-supported build, lint, type, or runtime validation.
- Final repository usability.

Executable behavior is primary where available. Semantic judgment remains required to detect missing requirements and unrelated changes that tests do not cover.

### Terminal Workflow

The rubric follows Terminal-Bench's final-state verification pattern.

Default criteria cover:

- Requested final environment state.
- Required files, services, database objects, or artifacts.
- Reproducibility or persisted configuration where requested.
- Resource, permission, network, and safety constraints.
- Unacceptable side effects.
- Read-only postcondition evidence for external operations.

The verifier evaluates final state rather than similarity to a reference command sequence.

### Repository Understanding

The rubric follows SWE-Atlas-QnA's task-specific factual pattern.

Default criteria cover:

- Positive claims that must be supported.
- Negative or contradictory claims that must be absent.
- Evidence traceability to current code, schema, or runtime observations.
- Multi-hop causal or architectural explanation.
- Completeness relative to the question.
- Repository preservation for analysis-only tasks.

Executable commands may collect or test evidence, but semantic judgment remains primary because a command cannot by itself establish explanatory completeness.

### Mixed Sessions

A mixed session has one high-level session rubric. Its criteria may reference results from several category-specific turn evaluators. The session rubric verifies integration and the final high-level outcome; it does not average category scores.

## Full Semantic Evaluation

### Evaluator Agent Interface

```text
evaluate_semantic(
  scope,
  frozen_rubric,
  evidence_manifest,
  project_snapshot,
  evaluator_config
) -> SemanticCriterionResults
```

The Codex app-server implementation uses a fresh ephemeral thread, read-only sandbox, fixed model and effort, strict output schema, and bounded evidence. The original task thread is never resumed for grading.

Evaluator configuration records:

- Backend and protocol version.
- Model and reasoning effort.
- Developer prompt and rubric-template versions.
- Output schema version.
- Project snapshot identity.
- Evidence-selection version.
- Start, finish, token usage, and failure diagnostics.

### Calibration and Adjudication

Most evaluations use one judge. Additional judgment is selective:

- Low evaluator confidence.
- Semantic and executable disagreement.
- High-risk task classification.
- New rubric or prompt version.
- Pairwise results near a tie.
- Random calibration sample.

A second judge returns an independent criterion result. An adjudicator receives both results and the original evidence, not private reasoning from either judge. Persistent disagreement is recorded rather than forced into false certainty.

Sparse human corrections calibrate judge agreement. Human review remains optional for ordinary sessions and required only for explicit governance cohorts or disputed high-impact evaluations.

## Full Executable Verification

### Snapshot Resolution

Historical verification must not run against an unrelated later checkout. The resolver prefers:

1. A recorded worktree or repository snapshot.
2. A reconstructable base commit plus captured patch.
3. A source fingerprint matching the current checkout.
4. Recorded command evidence when replay is impossible.

If none is available, the executable criterion remains `not_run` with an explicit reason. The system must not manufacture determinism from the wrong project state.

### Controlled Runner

The full runner supports isolated filesystem snapshots, bounded resources, timeouts, narrow environment inheritance, output and artifact limits, and pluggable parsers.

Side-effect classes are:

```text
read_only
local_build
sandboxed_local_mutation
external_read
external_mutation
destructive
unknown
```

Automatic execution is limited by policy. `external_mutation`, `destructive`, and `unknown` are never replayed solely for evaluation. Authorized mutation evidence may be retained, while later evaluation uses read-only postconditions.

### Reward Kit Pilot and Adoption

Harbor Reward Kit is the preferred reusable verifier candidate because it supports programmatic criteria, model or agent judges, workspace and trajectory inspection, partial rewards, and isolated criterion execution.

Adoption requires a compatibility gate:

- Dynamic database-backed rubrics can be rendered without losing provenance.
- Workspace paths do not require a Harbor-specific `/app` layout.
- Results map losslessly to `pass`, `partial`, `fail`, `unknown`, and executable error states.
- Criteria can reference CodingTrajectory evidence IDs.
- Execution isolation matches the side-effect policy.

If the gate fails, CodingTrajectory retains its controlled runner and uses Reward Kit's verifier patterns as a reference rather than forcing the dependency.

### Delayed Verification

Timing is an observation dimension, not a separate evaluator type.

Delayed executable observations may include:

- CI result after push.
- Deployment health after a defined window.
- Migration or schema compatibility.
- Non-reversion.
- Absence or presence of follow-up repair.
- Later regression evidence.

Every observation records mechanism, timing, window, source, result, and evidence. Evaluation lifecycle may move through:

```text
judged_resolved -> verified_resolved -> confirmed
verified_resolved -> regressed
```

Historical status is retained; delayed evidence appends a new lifecycle event rather than rewriting the original result.

## Optional Human Signals

Human signals are never mandatory for normal scoring.

Supported explicit values:

```text
accepted
rejected
corrected
partially_accepted
not_provided
```

The system may infer correction candidates from follow-up language, reverts, replacement changes, or later repair sessions. Inferred signals record source, confidence, and `explicit = false`. Absence of correction is not acceptance.

Human review is requested selectively for evaluator calibration, high-risk work, low confidence, unresolved disagreement, and small random audit samples.

## Difficulty

Difficulty records both initial and final estimates.

Five factors are scored from 0 to 2:

- Scope.
- Reasoning depth.
- Environment complexity.
- Requirement ambiguity.
- Verification burden.

The first exposed labels are `easy`, `medium`, `hard`, and `unknown`. Duration, tokens, cost, retries, and intervention are stored separately as observed effort.

After enough comparable attempts exist, an empirical model may estimate task difficulty independently from model capability. This model must be versioned and must not overwrite the original estimated difficulty.

## Model Performance

The primary report unit is a comparable cohort:

```text
project + category + difficulty + time window + harness/evaluator version
```

Required metrics:

- Verified resolve rate.
- Judged resolve rate.
- Partial and unverified rates.
- Evidence coverage.
- Regression rate when delayed evidence exists.
- Median wall time, tokens, and reported cost per verified resolution.
- Optional autonomy rate when correction or intervention evidence exists.

Reports always show sample count and confidence interval or uncertainty marker. They do not compare raw global percentages when task mixes differ materially.

A composite real-project score is deferred until category weights, difficulty normalization, minimum cohort size, and evaluator calibration are frozen. The underlying cohort metrics remain available even after a composite is introduced.

## Frozen Replay Benchmark

Accepted high-value daily tasks may become replayable benchmark cases.

```text
evaluated daily task
  -> accepted outcome and sufficient evidence
  -> frozen instruction, repository snapshot, rubric, and verifier
  -> contamination and secret review
  -> replay task version
  -> repeated attempts across models and harnesses
```

Harbor is the preferred replay target because it supports isolated task environments, multi-step tasks, artifacts, agent execution, verifier separation, and structured rewards.

Reference solutions are optional. When an accepted human or agent solution exists, it proves task solvability but is not used as exact-patch truth. Observable behavior and rubric criteria remain authoritative.

Replay reports include pass@1, pass@k when repeated attempts exist, result variance, cost variance, and failure-mode distribution.

## Historical Backfill and Continuous Evaluation

### Historical Backfill

Backfill proceeds by bounded project and date cohorts. It uses retrospective rubrics, records missing checkout or evidence reasons, and never blocks current ingestion.

Priority order:

1. Sessions with stable repository snapshots and recorded validation.
2. Sessions with clear user requirements and final outcomes.
3. Sessions selected for model or project comparison.
4. Ambiguous or incomplete sessions only after evaluator calibration improves.

### Continuous Evaluation

New sessions enter a queue after terminal session state or an inactivity threshold. Turn evaluations may run earlier, but session evaluation waits for a credible high-level boundary.

Rate limits, deduplication, and budgets are applied by source fingerprint and evaluator version. Failed evaluation jobs are retryable without changing canonical sessions.

## Storage and Versioning

Evaluation artifacts are immutable and append-only by evaluation ID. Mutable indexes point to the preferred artifact for a scope and evaluator policy.

Versioned surfaces include:

- Evaluation artifact schema.
- Category taxonomy.
- Difficulty policy.
- Rubric templates.
- Evidence builder.
- Semantic prompt and output schema.
- Evaluator model and effort.
- Executable runner and parser.
- Aggregation policy.
- Model-report cohort policy.

A new version creates new evaluations or projections. It does not silently reinterpret historical stored results.

## Security and Privacy

- Evidence packaging uses bounded slices and redacts secrets.
- Raw massive logs are queried and sampled, not copied wholesale.
- Evaluator prompts receive only evidence required by the rubric.
- Project snapshots and patches follow local retention policy.
- External mutations require existing task authorization and are never replayed by evaluation policy alone.
- Command output storage is bounded and hashed.
- Hosted judge backends require an explicit data-boundary decision before receiving private code or transcripts.

## Rollout

### Stage 1: Full rubric families

Implement category-specific templates, provenance, and selective second-judge adjudication. Re-run the Phase 1 cohort and freeze comparison results.

### Stage 2: Isolated executable verification

Add snapshot resolution, sandbox execution, parser plugins, and the Reward Kit compatibility pilot. Validate deterministic reproduction and side-effect enforcement.

### Stage 3: Continuous and delayed evaluation

Add evaluation scheduling, delayed observation lifecycle, optional human correction signals, and bounded historical backfill.

### Stage 4: Model reporting

Publish cohort reports by project, category, difficulty, model, and harness version. Require minimum sample sizes and visible uncertainty.

### Stage 5: Replay benchmark

Export selected tasks to Harbor, run repeated attempts across models, and calibrate empirical difficulty against real resolve rates.

## Full-System Acceptance Gates

- Category and resolution agreement remain above the frozen Phase 1 gates on a larger multi-provider audit cohort.
- Required executable criteria are reproducible from their declared snapshot or explicitly marked unavailable.
- No automatic evaluation path can perform an unapproved external or destructive mutation.
- Every score can be decomposed into rubric criteria and evidence.
- Judge-version changes are measured against a fixed calibration cohort before rollout.
- Human feedback remains optional and `not_provided` is never treated as acceptance.
- Delayed evidence changes lifecycle through append-only events.
- Model comparisons expose cohort composition, sample count, evaluator version, and uncertainty.
- Replay tasks preserve instructions, snapshots, verifier versions, and artifact provenance.

## Non-Goals

- A single exact solution for every real coding task.
- Exact patch matching as the primary correctness rule.
- Mandatory human labeling of daily sessions.
- Automated deployment or destructive replay for verification.
- Hiding evaluator disagreement behind one unexplained number.
- Ranking models globally before cohort normalization is defensible.

## Decision Summary

The full system scales the validated lightweight contracts rather than replacing them. Semantic judges interpret task-specific meaning; isolated executable verifiers prove observable postconditions; delayed and optional human evidence enrich the lifecycle; immutable provenance keeps results auditable; and replayable cases convert selected daily work into a durable private benchmark.
