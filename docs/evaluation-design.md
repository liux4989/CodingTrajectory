# CodingTrajectory Evaluation Mechanism

## Status

Active design contract for the replacement evaluation mechanism.

The dashboard-owned v2 evaluator was removed in commit `b178527` after its
CLI, HTTP, and UI entry points became orphaned. Its remaining high-level and
full-system documents are historical context, not implementation claims. This
design keeps the useful v2 ideas—bounded canonical evidence, frozen rubrics,
structured judging, and deterministic aggregation—but changes ownership,
storage, evidence semantics, and delivery order.

## Objective

Evaluate real coding-agent turns and sessions from local vendor logs while
answering two independent questions:

1. **Task outcome:** Did the observed work satisfy the specific request?
2. **Behavior adherence:** When a recurring situation occurred, did the agent
   follow the project's standing behavior specification?

The mechanism must produce auditable local artifacts, preserve uncertainty,
and remain usable without the dashboard or a hosted evaluation service.

## Why v2 Did Not Survive

The removed implementation proved one reduced-context turn, but it did not
complete its calibration cohort. Its architecture also made removal likely:

- One 1,900-line dashboard module owned contracts, evidence projection,
  judging, command execution, aggregation, persistence, CLI, and API behavior.
- Evaluation lifecycle was attached to dashboard jobs and navigation rather
  than an independently useful product surface.
- Loose JSON artifacts lived outside the revisioned storage architecture.
- Evidence selection depended partly on text heuristics such as recognizing
  validation commands by substrings.
- Historical checkout identity was inferred from final-response text and
  compared with the current worktree.
- The controlled runner could only establish a narrow current-checkout result,
  not faithfully replay historical state.
- The design claimed production progress after the implementation and its
  foundation document had been deleted.

The new mechanism is not a restoration of that module. It is a smaller,
independently owned subsystem with explicit stage boundaries.

## Product Boundary

Evaluation is a **derived projection** over canonical CodingTrajectory data.
It does not modify `SessionGraph`, vendor JSONL, or project files.

The initial owner is a dedicated executable plugin:

```text
packages/plugins/evaluation/
```

It consumes versioned `ct api` contracts like other plugins and does not import
core internals. The dashboard may later invoke and present its public commands,
but it does not own evaluation contracts, storage, jobs, or provider lifecycle.
Removing the dashboard must not remove evaluation.

The legacy `benchmarks/` package remains a tool-access experiment. It is not
the implementation base for session evaluation because it hard-codes task
types, invokes one agent harness, and primarily scores final text.

## Core Invariants

1. Vendor JSONL remains authoritative and read-only.
2. Canonical event, item, turn, session, and graph IDs are the primary evidence
   references.
3. A task contract is built from request-side evidence before outcome evidence
   is judged.
4. Every conclusive semantic result cites evidence included in the run.
5. `unknown` means insufficient evidence; `not_applicable` means the condition
   did not apply. Neither is silently converted to failure.
6. Recorded command results and newly replayed checks are different evidence
   classes and are never presented as each other.
7. Task resolution and behavior adherence are reported separately. A behavior
   failure blocks task resolution only when evaluation policy explicitly links
   that behavior to a required task criterion.
8. Completed artifacts are immutable and identified by all inputs that can
   change their meaning.
9. Judges receive bounded evidence, not raw logs, hidden reasoning, secrets, or
   unrestricted checkout access.
10. Evaluation never performs destructive or external mutation.

## Architecture

```text
Vendor JSONL
    │
    ▼
Canonical ct APIs ────────────────┐
    │                             │
    ▼                             ▼
Subject snapshot            BEHAVIOR.md discovery
    │                             │
    ├── request evidence          └── behavior spec set + digest
    └── outcome evidence
    │
    ▼
Task contract compiler
    │
    ▼
Frozen task rubric
    │
    ├──────────────┬─────────────────────────┐
    ▼              ▼                         ▼
Deterministic   Semantic outcome       Behavior adherence
graders         judge                  judge
    │              │                         │
    └──────────────┴─────────────────────────┘
                       │
                       ▼
              CT-owned aggregation
                       │
                       ▼
      Durable evaluation store + JSON export
```

### Component Responsibilities

```text
EvaluationService
  -> SubjectResolver
  -> EvidenceCatalogBuilder
  -> BehaviorSpecLoader
  -> TaskContractCompiler
  -> RubricCompiler
  -> DeterministicGraderRegistry
  -> SemanticJudge
  -> BehaviorJudge
  -> EvaluationAggregator
  -> EvaluationStore
```

Each component has typed Pydantic input and output. Provider adapters implement
one structured-completion interface; they do not own evaluation policy.

## Evaluation Units

### Turn

A turn is the first delivery unit. It has one material user request and the
agent work caused by that request. A turn may be:

- `evaluable`
- `not_applicable`
- `incomplete`

Acknowledgements, status-only exchanges, waits, and superseded requests with
no substantive work are `not_applicable`. Interrupted work with a substantive
request is `incomplete`, not `not_applicable`.

### Session

A session is evaluated against its high-level request and requirement changes.
It receives its own rubric and is not an average of turn scores. Turn results
are evidence and diagnostics for the session evaluator.

Session evaluation is added only after turn evaluation passes calibration.
This prevents multi-turn inference from hiding defects in the evidence and
grading contracts.

## Subject Snapshot and Identity

`EvaluationSubject` records:

```text
scope_type                 turn | session
scope_id                   canonical turn or session ID
root_session_id            owning graph ID
source_fingerprint         digest of canonical source references and content
source_complete            whether the observed trajectory is complete
project_identifier
observed_project_path      informational; never grants judge access
started_at / ended_at
vendor set
```

The logical evaluation key is a digest of:

```text
scope type and ID
source fingerprint
task-contract policy version
rubric template version
behavior spec-set digest
grader policy version
judge provider/model/effort/schema digest
aggregation policy version
```

An identical key reuses the completed artifact. A changed trace, spec, rubric,
judge, or policy creates a new run rather than rewriting history.

## Evidence Model

### Evidence Catalog

The catalog describes all evaluation-visible canonical evidence before prompt
selection. An `EvidenceRecord` contains:

```text
evidence_id                canonical ID, or deterministic ID for a derived fact
kind                       request | message | command | tool_call | file_change |
                           validation | repository_instruction | user_correction |
                           artifact | source_state
canonical_refs             event/item/turn/session IDs
occurred_at and sequence
content_sha256
bounded content or blob reference
metadata                   tool, path, exit status, provenance, truncation
sensitivity                normal | restricted | redacted
```

Canonical IDs are used directly whenever possible. Derived records use a
deterministic digest of their kind and canonical inputs; sequential labels such
as `artifact-003` are not durable identities.

Large content is stored once as a content-addressed local blob. Prompts receive
bounded excerpts with hashes and truncation metadata. Secret scanning and
redaction occur before persistence and judging.

### Request and Outcome Separation

The evidence catalog produces two projections:

- **Task-contract projection:** user requests, material corrections,
  applicable repository instructions, scope structure, and declared validation
  authority. It excludes final answers and observed success/failure.
- **Outcome projection:** final responses, tool results, command outcomes, file
  changes, validation evidence, contradictions, and user corrections.

The rubric is frozen from the task-contract projection before the outcome
projection is sent to any grader. Historical rubrics record
`origin = retrospective`; future capture may create prospective rubrics.

### Evidence Retrieval

The initial semantic prompt receives the rubric plus a bounded evidence index
and the smallest useful response/outcome slice. A judge may return one
structured evidence request containing known evidence kinds and canonical turn
IDs. CodingTrajectory—not the judge—resolves the request within a fixed budget
and runs one final fresh judging pass.

The judge cannot request raw JSONL, arbitrary paths, shell commands, or general
checkout access. Missing matching evidence leaves the criterion `unknown`.

## Standing Behavior Specifications

CodingTrajectory adopts the portable Agent Behavior directory convention:

```text
.agents/behaviors/<behavior-name>/BEHAVIOR.md
```

The required frontmatter is `name` and `description`; `license` and `metadata`
remain optional. Unknown frontmatter fields are preserved and ignored by the
portable parser. The body is free-form Markdown.

Initial CodingTrajectory rules are deliberately narrower than the external
examples:

- One behavior directory produces one behavior judgment.
- Behavior text is evaluation input, never automatically injected into the
  target agent's prompt.
- Scorer prompts, thresholds, and provider settings do not belong in
  `BEHAVIOR.md`.
- Specs are sparse and limited to recurring, consequential, trajectory-visible
  choices.

Each loaded spec records its relative path, normalized frontmatter, content
digest, and whether it was observed with the task or applied retrospectively
from the current project.

`BehaviorJudgment` returns:

```text
behavior_id
verdict                     true | false | not_applicable | unknown
evidence_ids
violated_clause             required for false
reason
confidence
```

The `unknown` extension is intentional. `not_applicable` means the triggering
situation did not occur; `unknown` means the available trajectory cannot prove
whether or how it occurred.

## Task Contract and Rubric

`TaskContract` contains the requested outcome, material constraints,
prohibitions, accepted requirement changes, repository instructions, and
scope-completeness information. Every statement cites request-side evidence.

`RubricCriterion` contains:

```text
criterion_id
success_statement
required
mechanism                   semantic | observed | replayed | combined
evidence_requirements
observable_postcondition
prohibitions
linked_behavior_ids
```

Category templates provide defaults for repository engineering, terminal
workflow, repository understanding, and mixed sessions. A structured model may
specialize the template, but CodingTrajectory validates that:

- every criterion traces to the task contract;
- IDs are unique and stable within the rubric;
- executable mechanisms name observable postconditions;
- linked behaviors exist in the frozen behavior spec set;
- the compiler did not cite outcome evidence.

Difficulty estimation and model leaderboards are not part of the first
mechanism. They are reporting projections added only after grading is trusted.

## Graders

### Deterministic Graders

Deterministic graders operate on typed canonical facts. Initial examples are:

- a command completed with an observed exit status;
- a validation occurred after the last relevant file change;
- a required artifact path was changed or inspected;
- a tool call has a paired result;
- the trace ended before task completion;
- a cited evidence ID exists and belongs to the evaluated scope.

They do not infer semantic task success from command names alone.

### Semantic Outcome Judge

The outcome judge returns one result per semantic criterion:

```text
pass | partial | fail | unknown
```

Every `pass`, `partial`, or `fail` cites evidence. Final-answer assertions are
claims, not proof. The judge reports contradictions and does not calculate the
overall resolution.

The initial provider is Codex app-server behind a generic structured-completion
interface. Every pass uses a fresh ephemeral thread, fixed model and effort,
strict output schema, `approvalPolicy = never`, and read-only sandboxing. Its
working directory is an evaluator-owned evidence directory, not the user's
project checkout. Evidence and specs are untrusted data and cannot authorize
tool use.

### Behavior Judge

The behavior judge evaluates observable process adherence independently from
task outcome. A lucky correct result can therefore pass its task rubric while
failing a behavior spec. Conversely, exploratory tool use does not fail merely
because it differs from a preferred implementation path.

### Executable Evidence

Phase 1 consumes **observed** executable evidence from the original trajectory;
it does not rerun commands against the current checkout. This avoids the v2
error of attributing today's repository state to historical work.

Later isolated replay may add `replayed` evidence only when CodingTrajectory has
a frozen repository snapshot or reconstructable base revision and patch. Replay
runs in a disposable environment with bounded resources and a side-effect
policy. External mutation, destructive operations, and unknown side effects
are never replayed for evaluation.

## Aggregation

Aggregation is deterministic and produces two result families.

### Task Result

Criterion states remain `pass`, `partial`, `fail`, `unknown`, and
`not_applicable`. Overall resolution is:

```text
verified_resolved       all required criteria pass with the required observed
                        or replayed executable proof
judged_resolved         all required criteria pass, but some executable proof
                        is unavailable
partial                 useful required achievement is incomplete
unresolved              at least one required criterion fails
unverified              required evidence is unknown
not_applicable          no evaluable task exists
```

Executable failure overrides a semantic claim about the same postcondition.
Executable success cannot satisfy an unrelated semantic requirement.

Achievement and evidence coverage may be reported separately, but the first
release does not publish one blended quality score.

### Behavior Result

Behavior results report counts and individual verdicts for true, false,
not-applicable, and unknown. `not_applicable` is excluded from adherence rates;
`unknown` remains visible as evidence coverage rather than being counted as
success or failure.

Behavior failure changes task resolution only through an explicit
`linked_behavior_ids` relationship on a required rubric criterion. This keeps
standing process standards from silently redefining historical task outcomes.

## Durable Storage

Evaluation cannot use the dashboard read-model database because that database
is disposable and intentionally garbage-collects history. The evaluation
plugin owns a separate versioned SQLite database under:

```text
~/.coding-trajectory/evaluations/evaluations-v1.sqlite3
```

Content-addressed evidence and large outputs live under:

```text
~/.coding-trajectory/evaluations/artifacts/sha256/<digest>
```

The database stores:

- logical run identity and state;
- immutable subject and source fingerprints;
- task contracts and frozen rubrics;
- behavior spec-set digests;
- evidence manifests and blob references;
- criterion and behavior judgments;
- grader invocations, schemas, versions, usage, and errors;
- aggregation results;
- calibration and human-review corrections.

Only queue/attempt state is mutable while a run is active. Completion publishes
all evaluation results in one transaction. A retry creates a new attempt; it
does not partially overwrite a completed artifact. Schema changes use explicit
migrations and never rebuild away durable evaluations.

JSON is an export and interoperability format, not the operational index.

## Lifecycle and Failure Model

```text
queued
  -> snapshotting
  -> compiling
  -> grading
  -> aggregating
  -> completed

Any active state -> failed | cancelled | stale
```

Each failure records its stage and whether retry is safe. Provider failure,
invalid model output, unavailable evidence, and task failure are distinct.
Invalid structured output receives at most one correction attempt; exhausted
repair marks the grader attempt failed rather than fabricating a task result.

Phase 1 runs synchronously from the CLI. Durable leased workers and continuous
scheduling are added only after calibration, so the mechanism has a useful
owner and interface before background infrastructure exists.

## Public Interface

The dedicated plugin initially exposes:

```text
ct plugin evaluation run --turn TURN_ID
ct plugin evaluation run --session SESSION_ID
ct plugin evaluation show EVALUATION_ID
ct plugin evaluation list --turn TURN_ID
ct plugin evaluation list --session SESSION_ID
ct plugin evaluation behaviors validate [PROJECT_PATH]
ct plugin evaluation calibrate MANIFEST
```

Commands support compact text and strict JSON output. The dashboard may later
proxy these operations and render stored results, but no dashboard route is an
evaluation source of truth.

## Privacy and Security

- Local storage and local judging are the default data boundary.
- Hosted judge or reporting adapters require an explicit opt-in.
- Environment values, credentials, and unrestricted tool configuration are not
  included in evidence.
- Evidence content is bounded, hashed, and redacted before judge invocation.
- Repository instructions, behavior Markdown, messages, and tool output are
  treated as untrusted data within evaluator prompts.
- Judges run outside the project checkout and cannot execute evidence content.
- Evaluation never resumes the original agent thread.
- Raw chain-of-thought is neither required nor accepted as correctness proof.

Braintrust may be added later as an export/reporting adapter. Its hosted store,
Gateway, and experiment runner are not runtime dependencies of the mechanism.

## Delivery Plan

### Phase 1: Turn Evaluation Core

Implement the dedicated plugin, Pydantic contracts, subject resolution,
evidence catalog, behavior loading, request/outcome separation, one semantic
provider, deterministic aggregation, durable store, and synchronous CLI.

Use observed command results only. Do not implement command replay, background
jobs, dashboard UI, global scoring, or historical backfill.

### Phase 2: Calibration and Deterministic Graders

Build a small sanitized calibration cohort, add typed deterministic graders,
measure evidence retrieval and judge agreement, and freeze the v1 schemas only
after the acceptance gates pass.

### Phase 3: Session Evaluation

Add requirement lineage, turn contributions, session-specific rubrics, and
session aggregation without averaging child-turn results.

### Phase 4: Isolated Replay

Add snapshot resolution, disposable verifier environments, bounded replay, and
explicit observed-versus-replayed provenance.

### Phase 5: Continuous Evaluation and Presentation

Add durable leases, scheduling, budgets, selective re-evaluation, dashboard
presentation, cohort reporting, and optional external exporters.

## Calibration Cohort

The first cohort contains 20–30 manually reviewed turns across Codex, Claude
Code, and Pi, including:

- repository engineering, terminal workflow, and repository understanding;
- completed, failed, interrupted, and not-applicable turns;
- traces with and without executable evidence;
- user corrections and requirement changes;
- behavior true, false, not-applicable, and unknown examples;
- lucky-correct task outcomes with behavior failures;
- incomplete traces where uncertainty must be preserved.

Reviewers compare both the bounded projection and the complete sanitized source
trajectory. Calibration labels and rationale are versioned data, not unit tests
or hard-coded production exceptions.

## Acceptance Gates

Phase 1 and calibration are complete only when:

- every completed evaluation is reproducible from its recorded identity inputs;
- repeated identical runs reuse the same logical result;
- 100% of conclusive semantic and behavior judgments cite valid in-scope
  evidence IDs;
- at least 95% of judge calls return schema-valid output without correction;
- task resolution and behavior verdict agree with human review on at least 85%
  of the calibration cohort, with disagreements retained and inspected;
- full-trajectory audit finds no systematic outcome-changing omission from the
  evidence retrieval policy;
- deterministic grader reruns produce identical outputs;
- `unknown`, `not_applicable`, provider failure, and task failure remain
  distinguishable in storage and reports;
- interruption at every stage leaves no partially published completed artifact;
- no evaluation path mutates vendor logs, project files, external systems, or
  canonical session data;
- the CLI remains fully useful with the dashboard absent.

## Explicit Non-Goals

- Recreating an executable agent from its session log.
- Treating an Agent File–style snapshot as the quick-read store.
- Replacing canonical `SessionGraph` with evaluation records.
- Exact patch matching as the primary correctness criterion.
- Grading hidden reasoning.
- Automatically replaying historical work against the current checkout.
- Prompting the target agent with behavior specs by default.
- One global quality score or model leaderboard before calibration.
- Braintrust, Harbor, or another hosted system as a required dependency.

## Decision Summary

Build evaluation as a dedicated local plugin over canonical CT APIs. Freeze a
request-derived task contract, evaluate task outcomes and standing behaviors as
separate result families, cite canonical evidence, preserve unknowns, and store
completed artifacts durably. Start with synchronous turn evaluation and
observed evidence; add sessions, replay, scheduling, and UI only after the core
contracts pass a reviewed calibration cohort.
