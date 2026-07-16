# Coding Session Evaluation: Foundation and Lightweight Implementation

## Status

Foundation implemented on 2026-07-15. The backend and CLI slice described below is available; the 20-to-30-session cohort gates remain pending. This document implements the contracts in [`session-evaluation-high-level-design.md`](session-evaluation-high-level-design.md) without committing to the scaling and replay features in [`session-evaluation-full.md`](session-evaluation-full.md).

## Objective

Build the smallest end-to-end evaluation framework that can:

- Evaluate both turns and sessions.
- Classify work as repository engineering, terminal workflow, repository understanding, or mixed at session scope.
- Freeze a task-specific rubric.
- Run a structured semantic evaluation through the existing Codex app-server adapter.
- Run a narrow set of safe executable checks.
- Aggregate criterion results into an inspectable resolution.
- Validate the mechanism on a deliberately small cohort before historical backfill or continuous evaluation.

Phase 1 proves the contract. It is not the full benchmarking product.

## Existing Repository Boundary

The canonical `ct api` service remains the source of truth for normalized sessions, turns, items, usage, tool evidence, project path, and request lineage. The dashboard plugin consumes those contracts and owns derived evaluation jobs.

The dashboard already owns one `CodexAppServerManager`, async `job_id` handling, ephemeral app-server threads, read-only evaluator execution, and strict structured output. Phase 1 extends those patterns rather than introducing a second agent runtime.

The first implementation lives in the dashboard plugin because that is where app-server lifecycle and long-running analysis jobs already exist. Evaluation artifacts remain derived records; they do not modify canonical session fields. Promotion to core service contracts is deferred until the Phase 1 schema is validated.

## Implemented Foundation

The Phase 1 backend lives in `packages/plugins/dashboard/evaluation.py` and provides:

- Strict Pydantic contracts for evidence, rubric compilation, semantic and executable criterion results, aggregation, identity, and persisted artifacts.
- Bounded evidence construction from canonical `session.overview` and `session.items` API projections, including stable evidence IDs and a source fingerprint.
- Deterministic eligibility classification before rubric compilation.
- A compact task contract for retrospective category and rubric compilation, separated from outcome evidence.
- A response-first semantic evidence selection capped independently from the retained audit manifest.
- One optional CT-controlled evidence-expansion round for `unknown` criteria, selected by canonical evidence kind and turn ID without raw-log access.
- One fresh structured app-server turn for rubric compilation and one fresh turn per semantic pass; CT does not implement a second agent loop or depend on implicit skill discovery.
- An allowlisted argument-array validation plan and controlled runner that refuses replay unless the recorded final revision matches the current clean checkout.
- Strict v2-only JSON artifacts and scope indexes under `~/.coding-trajectory/evaluations/v2/` by default, configurable through `CT_EVALUATION_ROOT`.
- Session and turn support through the dashboard CLI and asynchronous HTTP jobs.

Run a session evaluation with:

```text
uv run ct plugin dashboard session evaluate SESSION_ID
```

Run a turn evaluation by resolving it through its session graph:

```text
uv run ct plugin dashboard session evaluate SESSION_ID --turn TURN_ID
```

The implemented HTTP surface is:

```text
POST /api/evaluations/sessions/{session_id}
POST /api/evaluations/turns/{turn_id}
GET  /api/evaluations/{evaluation_id}
GET  /api/evaluations?scope_type=session&scope_id=...
```

No evaluation endpoint modifies canonical session data.

## First Reduced-Context Turn Run

The v2-only evaluator was validated on completed Codex turn `1a39e929-0b74-5b62-8ccd-1a65f83b54d3` from session `019f6594-8531-70f1-891f-6825f72eeec2`, the turn that originally implemented the lite foundation. The persisted evaluation is `eval_0c104a4fcdb20124c196bee6` under the v2 store.

The retained canonical audit manifest contained 12 evidence records and 12,548 evidence characters rather than the raw Codex log. The rubric compiler input was 5,717 characters. The response-first semantic judge input was 21,282 characters including instructions, rubric, JSON structure, and selected evidence. The run returned `judged_resolved` with all five criteria passing, rubric score `1.0`, and evidence coverage `1.0`. No expansion was requested; executable proof remained unavailable because the historical checkout could not be replayed from the current dirty source state.

This turn run validates the strict v2 schema, reduced compiler context, response-first semantic selection, direct validation recognition, evidence references, immutable v2 persistence, and turn-scope execution. It does not validate a second semantic pass because the first pass had sufficient evidence, and it does not satisfy the cohort acceptance gates.

## Phase 1 Components

```text
EvaluationService
  -> EvaluationEligibility
  -> EvaluationInputBuilder
  -> RubricCompiler
  -> SemanticEvaluator
  -> ValidationPlanBuilder
  -> LiteExecutableRunner
  -> EvaluationAggregator
  -> EvaluationStore
```

`EvaluationService` is the coordinator. It owns state transitions and invokes components; it does not duplicate canonical session reconstruction.

The implemented v2 evaluator keeps the full retained evidence manifest capped at 80,000 characters for provenance, caps the compiler task contract at 24,000 evidence characters, caps the initial semantic selection at 30,000 evidence characters, and caps one expansion at 16,000 evidence characters. Prompt and schema overhead are recorded separately through each app-server invocation's input character count.

## Domain Contracts

The implementation should use strict Pydantic models and version every persisted top-level artifact.

### Evaluation Identity

```text
evaluation_id       stable ID for one scope, source revision, and evaluator contract
scope_type          turn | session
scope_id            canonical turn or session ID
source_fingerprint  hash of normalized source evidence used by the evaluation
rubric_version      rubric schema and template version
evaluator_version   prompt, output schema, model, effort, and executable-runner version
created_at
```

Re-evaluating an unchanged scope with the same versions may reuse the artifact. A changed source fingerprint, rubric revision, or evaluator version creates a distinct evaluation.

### Category Result

```json
{
  "primary": "repository_understanding",
  "secondary": ["terminal_workflow"],
  "confidence": 0.88,
  "reason": "The requested outcome is an evidence-backed diagnosis; terminal commands only collect evidence."
}
```

Only sessions may use `mixed` as primary. A turn with uncertain classification uses the best-supported primary category plus confidence rather than `mixed` by default.

### Rubric Criterion

```json
{
  "criterion_id": "identify-upstream-cause",
  "statement": "Identifies the first upstream condition that prevents Stage 1 execution",
  "required": true,
  "weight": 3,
  "mechanism": "semantic",
  "evidence_requirements": ["current code path", "runtime or schema evidence"],
  "prohibitions": ["do not attribute the failure to concurrency without evidence"]
}
```

Phase 1 supports `semantic`, `executable`, `both`, and `human_optional`. Human criteria are stored but never block automatic resolution.

### Criterion Result

```json
{
  "criterion_id": "identify-upstream-cause",
  "mechanism": "semantic",
  "result": "pass",
  "confidence": 0.86,
  "evidence_ids": ["request-1", "code-path-4", "runtime-2"],
  "reason": "The conclusion follows the current prerequisite chain and is supported by the captured failure."
}
```

Executable results use `pass`, `fail`, `error`, `timeout`, or `not_run`. Aggregation normalizes `error`, `timeout`, and required `not_run` to unresolved or unverified according to whether the criterion could be evaluated semantically.

## Eligibility

The eligibility classifier runs before rubric generation.

Evaluable units include implementation, analysis, terminal work, review, design, and documentation tasks with an observable requested outcome.

Units are `not_applicable` when they are only:

- Acknowledgement.
- Status request.
- Clarification without attempted work.
- Wait or monitoring update without a new decision.
- Superseded before substantive work.
- Pure social conversation.

Eligibility output must include a reason and confidence. Low-confidence cases remain visible for cohort review rather than silently disappearing.

## Evaluation Input Builder

The input builder creates a bounded evidence package from canonical `ct api` projections. It must not load or copy a massive raw log into the evaluator prompt.

The builder uses count, query, and sample operations first, then selects the smallest evidence slice needed for the rubric:

- Initial request and material follow-up requirements.
- Turn summaries and contribution relationships.
- Relevant agent claims and final response.
- Tool and command summaries.
- Touched artifact paths.
- Bounded diffs or change summaries when available.
- Recorded validation commands and outcomes.
- User corrections or explicit acceptance when present.
- Project path and repository instructions.

Every included item receives an evidence ID and source reference. Oversized command output is represented by exit code, count, bounded head/tail samples, and an artifact pointer rather than raw inclusion.

## Category and Rubric Compilation

Phase 1 may use one structured Codex app-server turn to produce title, eligibility confirmation, category, difficulty estimate, and draft rubric. The response schema must be strict and versioned.

The compiler receives a compact task contract containing the requests, bounded turn structure, applicable repository instructions, and sanitized validation authority. It does not receive the full outcome evidence manifest. This prevents duplicated context and reduces outcome leakage into retrospective rubric construction.

Rubric compilation follows these rules:

1. The requested outcome determines category.
2. Category selects a rubric template family.
3. The compiler specializes criteria using explicit requirements and available project evidence.
4. Required criteria must be concrete enough to evaluate.
5. Executable criteria must name an observable postcondition, not merely a command.
6. The rubric is frozen before the final semantic judgment and executable aggregation.
7. A later requirement change creates a rubric revision with a reason.

For historical sessions the rubric is necessarily reconstructed after execution. Those artifacts record `rubric_origin = retrospective` and lower provenance confidence. New live sessions may record `rubric_origin = prospective` once pre-execution capture exists.

## Lite Semantic Evaluator

The semantic evaluator reuses `CodexAppServerManager` through a specialized evaluation invoker.

Each evaluated scope starts a fresh ephemeral app-server thread. It must not resume the original coding thread or reuse the target agent's context. The thread uses:

```text
cwd             recorded project path when available
sandbox         read-only
approvalPolicy  never
model           fixed evaluator model for the cohort
effort          fixed evaluator effort for the cohort
outputSchema    SemanticEvaluationResult schema
```

The initial prompt contains the frozen rubric and a response-first evidence selection: requests, recent agent response evidence, checkout identity, compact turn summaries, validation and artifact evidence, tool summaries, and repository instructions within a separate semantic budget. It instructs the evaluator to judge only supported observable evidence, identify contradictions, use `unknown` when evidence is insufficient, and return evidence IDs for every pass or fail.

For an `unknown` criterion the evaluator may request missing evidence using a criterion ID, one to three canonical evidence kinds, and optional turn IDs. CT accepts requests only for unknown criteria, selects unseen canonical evidence within the expansion budget, and runs one final fresh semantic turn. The final pass cannot trigger another expansion. If no matching evidence exists or the budget is exhausted, the result remains `unknown`; the evaluator never falls back to the full raw log.

The semantic evaluator does not choose the final session resolution. It returns criterion results to the aggregator.

Phase 1 uses one judge with at most one evidence-expansion pass. A second independent judge, ensemble, adjudication, and model calibration belong to Phase 2.

## Lite Executable Verification

### Validation Discovery

The validation-plan builder may propose checks from:

1. Explicit user acceptance criteria.
2. Successful validation commands recorded in the session.
3. The applicable `AGENTS.md` instructions.
4. Package-manager scripts and existing project commands.
5. Existing CI, build, lint, or read-only health commands.

Codex app-server may generate a structured candidate plan, but it does not authorize or execute the plan.

### Validation Specification

```json
{
  "validation_id": "frontend-build",
  "argv": ["bun", "run", "build"],
  "cwd": "frontend",
  "timeout_seconds": 300,
  "expected_exit_codes": [0],
  "side_effect": "local_build",
  "network_required": false,
  "supports_criteria": ["frontend-compiles"],
  "source": "applicable AGENTS.md"
}
```

Argument arrays are preferred over shell strings. Environment inheritance is narrow and secrets are not copied into recorded evidence.

### Allowed Phase 1 Operations

Automatically runnable:

- Read-only inspection.
- Local compile, lint, type check, or build explicitly supported by the project.
- Safe local validation already run in the session.
- `git diff --check` and equivalent repository hygiene.

Not automatically runnable:

- Deployment.
- Database mutation or migration.
- Destructive reset or cleanup.
- External messages or writes.
- Commands requiring unknown credentials.
- Commands whose side effects cannot be classified.

Phase 1 runs checks against the current recorded project checkout only when its fingerprint matches the expected source state. Otherwise it records the check as unavailable rather than attributing a later checkout's result to the historical session.

### Runner Result

The runner captures command identity, cwd, start and end time, exit code, timeout, bounded output samples, output hash, and supported criterion IDs. A command succeeding does not directly mark all linked criteria as passed; the criterion's declared postcondition and parser determine the result.

## Aggregation

Criterion achievement values are:

```text
pass    1.0
partial 0.5
fail    0.0
unknown 0.0
```

The framework reports both weighted achievement and evidence coverage so unknown evidence is not confused with proven failure.

```text
rubric_score = weighted achievement / total applicable weight
evidence_coverage = weight with sufficient evidence / total applicable weight
```

Resolution rules:

- `verified_resolved`: every required criterion passes and every required executable criterion has passing executable evidence.
- `judged_resolved`: every required criterion passes semantically, but executable proof is unavailable for at least one otherwise satisfied criterion.
- `partial`: no hard contradiction makes the result wholly invalid, but required coverage is incomplete.
- `unresolved`: at least one required criterion fails or a required executable check fails.
- `unverified`: required evidence is unknown and there is insufficient support for partial or resolved status.

When semantic and executable results conflict on the same observable condition, executable failure wins. Executable success remains scoped to its declared condition and cannot fill unrelated semantic criteria.

## Turn and Session Flow

Turn evaluations run independently. Session evaluation then receives:

- The high-level session request and requirement revisions.
- The session-specific frozen rubric.
- Child-turn categories, contributions, and evidence references.
- Child-turn executable results.
- Final session response and final project evidence.

The session evaluator judges its own rubric. Child-turn scores are diagnostic inputs and are never averaged into the session score.

## Storage and API Shape

Phase 1 uses a dashboard-owned `EvaluationStore` with a configurable root and a default under `~/.coding-trajectory/evaluations/v2/`. Each evaluation is a strict schema-v2 immutable JSON artifact keyed by evaluation ID, with a schema-v2 index from canonical scope ID to available evaluation versions. The implementation does not read or migrate v1 artifacts.

The store retains:

- Identity and source fingerprint.
- Category, title, and difficulty estimate.
- Frozen rubric and provenance.
- Evidence manifest and artifact references.
- Semantic and executable criterion results.
- Aggregated resolution.
- Evaluator configuration and diagnostics.

Suggested asynchronous dashboard endpoints:

```text
POST /api/evaluations/turns/{turn_id}
POST /api/evaluations/sessions/{session_id}
GET  /api/evaluations/{evaluation_id}
GET  /api/evaluations?scope_id=...
```

POST returns `202` with a dashboard `job_id`. Existing job polling owns in-flight recovery. Evaluation IDs are durable artifact identities; raw app-server thread and turn IDs remain diagnostics only.

No frontend implementation is required to validate Phase 1. A narrow API or CLI-facing report is sufficient for the first cohort.

## Lightweight Validation Cohort

Select approximately 20 to 30 sessions rather than backfilling all logs. Include:

- All three task categories.
- Mixed sessions.
- Focused single-turn and long multi-turn work.
- Resolved, blocked, failed, and abandoned outcomes.
- Sessions with and without recorded validation.
- Sessions with user correction.
- Multiple providers and models already supported by CodingTrajectory.

The cohort manifest freezes canonical session IDs, project paths or unavailable-path reason, expected eligibility, and review owner. It must not contain secret or massive raw transcript copies.

## Phase 1 Acceptance Gates

- Every persisted evaluation references a versioned rubric and source fingerprint.
- At least 95% of semantic evaluator turns return schema-valid results without manual repair.
- Every semantic `pass` or `fail` references evidence.
- Safe executable checks reproduce recorded outcomes in at least 95% of eligible matching checkouts.
- No destructive or external mutation executes automatically.
- Semantic and executable disagreement remains explicit in the artifact.
- Session resolution is produced from a session rubric, not turn-score averaging.
- A bounded human review agrees with primary category and resolution on at least 80% of the cohort.
- Re-running unchanged executable checks is deterministic; semantic variance is measured and retained.
- Evaluation failure never changes canonical session data or project files beyond allowed local build artifacts.

Human review is a Phase 1 quality gate for the small cohort, not a permanent requirement for every evaluated session.

## Implementation Order

1. Add Pydantic evaluation contracts and JSON serialization.
2. Add eligibility and bounded evaluation-input projection.
3. Add category and rubric compilation through a fresh app-server thread.
4. Add semantic criterion evaluation through a second fresh app-server thread.
5. Add validation-plan contracts and side-effect classification.
6. Add the narrow controlled runner and result parsers.
7. Add aggregation and immutable evaluation storage.
8. Expose asynchronous evaluation jobs.
9. Run the lightweight cohort and revise contracts from observed failures.
10. Freeze the Phase 1 schema before beginning the full evaluation implementation.

## Phase 1 Non-Goals

- Full historical backfill.
- Continuous automatic evaluation of every new session.
- Reward Kit or Harbor as required dependencies.
- Multi-judge consensus.
- External mutation replay.
- Delayed production verification.
- Pairwise model comparison.
- Empirical difficulty or a global model score.
- Requiring human feedback on ordinary evaluations.

## Exit Decision

Phase 1 is complete when the framework can show, for every cohort item, what was requested, how it was categorized, which criteria were frozen, which evidence was inspected, which safe checks ran, why each criterion passed or failed, and how the turn or session resolution followed from those results.
