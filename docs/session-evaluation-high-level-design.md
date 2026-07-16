# Coding Session Evaluation: High-Level Design

## Status

Active architecture contract. The strict v2 Phase 1 backend and CLI foundation is implemented and has completed its first reduced-context turn evaluation; cohort validation is still pending. The lightweight implementation and current run record are specified in [`session-evaluation-foundation-lite.md`](session-evaluation-foundation-lite.md), and the production system is specified in [`session-evaluation-full.md`](session-evaluation-full.md).

## Purpose

CodingTrajectory should turn daily coding-agent sessions into a private, real-project benchmark. Public benchmarks measure models on fixed general datasets. This mechanism measures how models perform on the user's actual repositories, constraints, workflows, and long-horizon tasks.

The mechanism adds three derived properties to coding work:

- A concise task label and title.
- A benchmark-aligned coding-task category.
- Evidence-backed evaluation at both turn and session scope.

The evaluation must reward verified project outcomes rather than activity volume, persuasive final answers, or token usage.

## Product Questions

The system should answer:

- Which models complete repository engineering, terminal workflows, and repository-understanding tasks most reliably?
- How does performance change by project, category, and difficulty?
- Does a model perform well on focused turns but poorly on long-horizon sessions?
- Which results are executable proof, which are rubric judgments, and which remain unverified?
- What does a verified completion cost in time and tokens?

## Evaluation Units

### Turn

A turn is one user request followed by the complete agent work episode caused by that request. It includes visible agent messages, tool calls, command runs, file changes, verification, and the final response.

A turn is the focused evaluation unit. It measures instruction following, local reasoning, tool use, requirement coverage, and focused task completion.

Not every conversational turn is evaluable. Acknowledgements, status-only requests, clarification-only exchanges, waiting updates, and interrupted turns may be marked `not_applicable`.

### Session

A session is the high-level task composed from multiple turns. It measures decomposition, cross-turn consistency, recovery, integration, context retention, and final project outcome.

The session receives its own rubric and result. Its score is not an average of child-turn scores. Failed exploration may lead to a resolved session, while many successful supporting turns cannot hide one failed critical requirement.

Each evaluated turn records its contribution as `critical`, `supporting`, `exploratory`, or `superseded` so turn results can explain the session result without mechanically determining it.

## Task Categories

The primary taxonomy follows three complementary public benchmark profiles.

| Category | Benchmark reference | Requested outcome | Common daily tasks |
| --- | --- | --- | --- |
| `repository_engineering` | DeepSWE | A working repository change | Features, bug fixes, refactors, tests, implementation-bound documentation |
| `terminal_workflow` | Terminal-Bench v2 | A correct environment state or produced artifact | Build, CI, deployment, data processing, system repair, administration |
| `repository_understanding` | SWE-Atlas-QnA | An evidence-backed technical explanation | Root-cause analysis, architecture, onboarding, code tracing, review |

A turn should normally have one primary category. A session may be `mixed` when independent categories materially contribute to the high-level result. Category describes the requested outcome, not every technique used during the work.

The external benchmarks provide the evaluation pattern, not a universal rubric. Their individual tasks have task-specific tests, verifiers, or factual criteria. CodingTrajectory therefore uses category-specific rubric templates to create a frozen rubric for each real task.

## Evaluation Mechanisms

Task category and evaluation mechanism are separate concepts. The system has two primary evaluation mechanisms.

### Semantic Trajectory Evaluation

An independent evaluator analyzes the observable trajectory against a frozen rubric. It considers the request, requirement changes, visible reasoning, inspected evidence, changed artifacts, validation attempts, user corrections, and final response.

The evaluator does not receive the full raw vendor log. CT reconstructs the canonical session graph, freezes a compact task contract, and selects criterion-relevant evidence records. The initial judge pass is response-first; when a criterion remains `unknown`, the judge may request one bounded expansion by canonical evidence kind and turn ID. CT validates and resolves that request without granting raw-log or unrestricted checkout access.

Semantic evaluation is necessary for requirement coverage, architecture, causal explanations, scope discipline, and other criteria that cannot be reduced to a deterministic command. It returns `pass`, `partial`, `fail`, or `unknown` with evidence references and confidence.

Private chain-of-thought is not required and is not treated as proof. The evaluator judges observable claims, actions, evidence, and outcomes.

### Executable Verification

A controlled runner executes safe project validation operations or inspects observable state. Candidate operations come from the user contract, `AGENTS.md`, package scripts, CI configuration, commands recorded in the session, and project-specific health checks.

Executable verification is stronger but narrower than semantic evaluation. A successful build proves that the project builds under the checked conditions; it does not prove that every requested feature was implemented.

Unsafe, destructive, or external mutations are not replayed merely for evaluation. External work is evaluated through authorized execution evidence or read-only postcondition checks.

### Contrast

| Dimension | Semantic evaluation | Executable verification |
| --- | --- | --- |
| Main question | Does the work logically satisfy the requirement? | Does an observable postcondition pass? |
| Evaluator | Independent LLM agent, optionally human | Controlled process runner and result parser |
| Strength | Broad meaning and requirement coverage | Stronger deterministic evidence |
| Weakness | Judge variance and bias | Limited to checked behavior |
| Result | Judgment plus confidence | Pass, fail, error, or timeout |
| Typical use | Explanation, causality, scope, completeness | Tests, builds, files, schema, runtime state |

Most real tasks use both. Repository engineering and terminal workflows normally emphasize executable evidence, while repository understanding normally emphasizes semantic evidence.

## Rubric Contract

Every evaluated turn or session has a versioned task-specific rubric frozen before final evaluation. Each criterion declares:

- A stable criterion ID.
- A concrete success statement.
- Whether it is required.
- Its weight.
- Its verification mechanism: `semantic`, `executable`, `both`, or `human_optional`.
- Required evidence or observable postcondition.
- Any prohibited claim, change, or side effect.

Requirement changes create a new rubric revision with provenance. The system must not silently rewrite criteria after seeing the answer in order to make the result pass.

## Evidence and Resolution

Evaluation conclusions reference immutable evidence records rather than embedding unsupported summaries. Evidence may include normalized requests, agent messages, file inspections, diffs, command results, builds, tests, runtime state, user corrections, and later regressions.

The common criterion states are:

```text
pass
partial
fail
unknown
not_applicable
```

The common evaluation states are:

```text
verified_resolved   all required criteria pass with required executable evidence
judged_resolved     all required criteria pass, but some executable proof is unavailable
partial             useful progress without full requirement coverage
unresolved          at least one required criterion fails
unverified          evidence is insufficient for a resolution decision
not_applicable      the unit is not an evaluable coding task
```

Executable failure overrides a semantic claim about the same observable behavior. Executable success does not override a missing semantic requirement.

Verification timing is independent from mechanism. Immediate checks run when work ends. Delayed checks may later confirm CI, deployment health, non-reversion, or regression. A result may evolve from `judged_resolved` to `verified_resolved` to `confirmed`, or move to `regressed` when later evidence contradicts it.

Human feedback is optional. Explicit or inferred corrections enrich an evaluation, but absence of feedback is recorded as `not_provided`, never as acceptance.

## Architecture

```text
Canonical ct session graph
  -> evaluation eligibility
  -> compact task contract
  -> task category and task-specific frozen rubric through Codex app-server
  -> criterion-focused evidence bundle
      -> semantic evaluator through a fresh Codex app-server turn
      -> optional single bounded evidence expansion
      -> executable verifier through controlled runner
  -> criterion result aggregation
  -> turn or session evaluation artifact
  -> project/category/difficulty model reports
```

The canonical core remains the source of truth for sessions, turns, items, usage, and tool evidence. Evaluation is a versioned derived analysis. The dashboard server owns evaluation jobs and Codex app-server lifecycle. Raw app-server thread IDs do not become evaluation identities.

CT owns scheduling, evidence budgets, expansion authorization, executable safety, aggregation, and persistence. Codex app-server owns the semantic reasoning turn. The system does not implement a second general-purpose agent loop, and production evaluation does not depend on implicit skill discovery.

## Difficulty and Model Performance

Difficulty is independent from model effort. It begins as an estimated task property derived from scope, reasoning depth, environment complexity, ambiguity, and verification burden. Tokens, duration, retries, and tool calls remain separate execution-effort metrics.

Model reports should lead with cohort metrics rather than one global score:

- Verified completion rate.
- Judged completion rate.
- Autonomous completion rate when intervention evidence exists.
- Reopen or regression rate when delayed evidence exists.
- Median time, tokens, and cost per verified completion.
- Results grouped by project, task category, difficulty, model, and harness version.

The system may derive empirical difficulty and a normalized composite score only after enough comparable attempts exist.

## Delivery Phases

### Phase 1: Foundation and Lightweight Evaluation

Build the common contracts and validate lite versions of both evaluation mechanisms on a small, diverse session cohort. Codex app-server supplies the semantic evaluator and validation-plan agent. A narrow controlled runner executes only safe, already-supported project checks.

Phase 1 asks: can one turn or session be evaluated consistently, and can every conclusion be traced to evidence?

### Phase 2: Full Evaluation

Add richer rubric families, isolated snapshots, full executable verification, optional Reward Kit integration, delayed outcomes, model comparison, repeated replay, historical backfill, and production reporting.

Phase 2 asks: can the system evaluate daily usage at scale and compare models fairly?

## Non-Goals

- Replacing the canonical session graph with evaluation-specific records.
- Treating hidden reasoning as correctness evidence.
- Treating build success as complete task success.
- Requiring human review for every session.
- Automatically replaying destructive or external mutations.
- Producing a global model leaderboard before category and difficulty normalization are credible.

## Decision Summary

Evaluate both focused turns and complete sessions. Classify work through benchmark-aligned task categories. Build task-specific rubrics from category-specific evaluation patterns. Use semantic trajectory evaluation for meaning and executable verification for observable proof. Preserve both evidence channels, their confidence, and their timing. Validate the contracts in a lightweight phase before applying the full system to historical and continuous daily usage.
