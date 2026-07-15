# Metrics Validation Quality Gate Design

## Status

Proposed design. This document defines the quality gate before implementation.

## Purpose

CodingTrajectory deliberately does not use unit tests in this repository, but core metric changes still need a repeatable correctness gate. The gate will use audited historical coding sessions from multiple providers as immutable baseline evidence and compare current `ct` metric projections against independently reconstructed expectations.

The gate is not a snapshot test that records whatever the current code emits. A coding agent must first inspect the source JSONL and independently derive the expected values. Only an audited expectation can become an active baseline.

## Goals

- Detect semantic regressions in token usage, cost evidence, runtime, turn counts, graph aggregation, and model attribution when the core ingestion or metric layer changes.
- Exercise real historical behaviors from Codex CLI, Claude Code, and Pi instead of synthetic one-field examples.
- Preserve the distinction between provider evidence, normalized canonical facts, and public `ct` projections.
- Produce an actionable field-level diff instead of a generic pass or fail.
- Run through one repository command and become a required gate whenever relevant core paths change.
- Allow intentional metric-contract changes without silently blessing new output.

## Non-goals

- This is not a unit-test suite.
- This does not evaluate whether an agent completed its software-engineering task successfully.
- This does not benchmark providers or models against one another.
- This does not replace static analysis, import checks, or frontend builds.
- This does not commit unredacted user session logs, credentials, environment values, or proprietary source content.
- This does not require live provider APIs or current model pricing during recurring validation.

## Quality Assertion

For every active baseline case, the same immutable source evidence and pinned pricing inputs must produce the audited canonical metrics and public service projections.

```text
historical JSONL evidence
  -> provider adapter
  -> canonical session graph
  -> metric analysis
  -> public ct contract
  -> normalized comparison
  -> pass or field-level regression report
```

The recurring gate verifies deterministic transformation of evidence. It does not claim that the historical provider log is complete beyond the fields actually present in that log.

## Baseline Case Selection

The first baseline cohort should be small enough to audit deeply and broad enough to cover the known semantic boundaries. A case is selected for a behavior, not merely because it is a convenient recent session.

| Coverage dimension | Minimum initial case |
| --- | --- |
| Provider | One Codex CLI, one Claude Code, and one Pi session graph |
| Graph shape | One single-session graph and one graph containing a subagent or sidechain |
| Token accounting | Fresh input, cached input, cache write when available, completion, and reasoning tokens |
| Model attribution | One single-model graph and one graph containing a model or effort change |
| Runtime | Multiple turns with measurable execution and wait intervals |
| Tool lifecycle | Successful tool calls and at least one failed or interrupted operation |
| Context lifecycle | One compaction or cache-boundary case when the provider exposes it |
| Cost evidence | One provider-reported cost case and one pinned estimated-cost case when available |

One historical graph may satisfy several dimensions. The manifest must state exactly why each case exists so later cleanup does not remove apparently redundant coverage.

## Evidence Bundle

Recurring validation must be portable and must not depend on a developer's live `~/.codex`, `~/.claude`, or `~/.pi` directories. Each approved case becomes a committed, sanitized evidence bundle.

Proposed layout:

```text
validation/metrics/
  manifest.toml
  pricing/
    pinned-model-prices.json
  cases/
    <case-id>/
      provenance.json
      source/
        *.jsonl
      expected/
        session-overview.json
        session-stats.json
        session-usage.json
        session-model-usage.json
      audit.md
scripts/
  validate-metrics-baselines.py
  check-metrics-quality-gate.sh
```

The implementation may adjust filenames, but the separation between source evidence, expected projections, provenance, and audit reasoning is required.

### Provenance

`provenance.json` records:

- baseline case ID and status;
- provider and adapter family;
- original source hash before sanitization;
- committed source hash after sanitization;
- sanitization procedure version;
- session graph entry-point ID;
- selected coverage dimensions;
- expected-output schema versions;
- pinned pricing artifact version when cost is in scope;
- original audit date and auditor agent identity;
- last intentional contract migration, if any.

The original source path may be recorded in a private audit note during case creation, but it must not be required by the committed recurring gate.

### Sanitization

Sanitization removes or replaces secrets and unrelated user content while retaining every field needed to reproduce the selected metric behaviors. It must preserve event order, timestamps, usage observations, model/provider identifiers, session relationships, tool statuses, runtime observations, and any content length needed by a metric under validation.

Sanitization is allowed to change stable IDs only if all affected references are deterministically rewritten together. The audit must confirm that the sanitized bundle still reproduces the independently derived metric expectations.

## Independent Baseline Audit

The initial audit prevents current implementation bugs from becoming accepted baselines.

### Pass 1: Source-only reconstruction

A fresh coding-agent thread receives:

- the sanitized JSONL evidence;
- the provider token semantics documented in `docs/token-usage-glossary.md`;
- the public metric definitions under review;
- the pinned pricing table when cost is included.

It does not receive the current `ct` output during the first pass. The agent reconstructs:

- session and graph membership;
- turn boundaries and statuses;
- provider usage observations;
- normalized token buckets;
- graph, session, and turn totals;
- model/provider grouping;
- execution and wait durations;
- tool and interruption counts;
- reported or estimated cost evidence.

The resulting derivation is written to `audit.md` with explicit arithmetic and source-event references.

### Pass 2: Implementation comparison

The agent then runs the current public surfaces against the evidence bundle:

```text
ct session overview <id> --output json
ct session stats <id> --output json
ct session usage <id> --output json
ct api call session.model_usage --params '{"session_id":"<id>"}'
```

Current output is compared with the source-only reconstruction. A baseline is approved only after discrepancies are resolved as one of:

- implementation defect fixed before approval;
- audit calculation corrected with evidence;
- documented provider limitation represented as missing or lower-confidence data;
- intentional public-contract rule added to the audit.

### Pass 3: Cross-check

A second agent or human reviewer checks the arithmetic, source references, sanitization safety, and expected JSON. The reviewer must not approve by merely observing that current and expected JSON match.

## Expected Output Policy

Expected JSON contains the stable public facts required by the case. It should omit irrelevant presentation fields so an unrelated wording or ordering change does not invalidate the gate.

Comparison rules:

- identifiers, counts, token integers, statuses, relationships, and normalized enum values compare exactly;
- timestamps compare exactly after canonical UTC serialization;
- durations compare exactly when derived from fixed timestamps;
- USD values compare at the precision declared by the core cost contract;
- list ordering compares only where ordering is part of the public contract;
- absent evidence remains absent and is never coerced to zero;
- warnings expected by the case compare explicitly;
- graph totals, main-session values, and subagent values remain separate.

The verifier uses Pydantic models to validate the manifest, provenance, expected output, and comparison report before evaluating values.

## Cost Stability

Recurring validation must not depend on the live models.dev catalog. Cost cases use a committed pricing snapshot or provider-reported cost from the immutable source evidence.

Usage correctness and pricing correctness are reported separately:

```text
usage gate: observed and normalized token buckets
pricing gate: pinned rates applied to the audited usage buckets
```

An updated market price is not a metric regression. Updating the pinned pricing artifact is an explicit baseline-contract change with its own audit entry.

## Recurring Gate

The primary command is proposed as:

```text
uv run python scripts/validate-metrics-baselines.py
```

It performs the following steps:

1. Validate the baseline manifest and every evidence bundle.
2. Load only the committed evidence paths, never the user's live discovery roots.
3. Run the current core ingestion and public service projections.
4. Normalize only fields declared non-semantic by the baseline contract.
5. Compare actual and expected values.
6. Verify cross-field invariants.
7. Emit a concise terminal report and a machine-readable JSON report.
8. Exit nonzero on any unexplained difference, missing case, invalid artifact, or invariant failure.

Required invariants include:

- graph token totals reconcile with their canonical session and turn sources according to the documented aggregation rule;
- main and subagent sections remain distinct from the graph aggregate;
- processed-token accounting uses canonical uncached, cached, cache-write, completion, and reasoning semantics;
- cost is absent when required pricing evidence is absent;
- a graph cost is not silently presented as complete when one attributed model is unpriced;
- execution time, wait time, and elapsed timestamp span are not conflated;
- failed tool calls and interrupted turns come from their canonical event or runtime observations.

## Automatic Trigger

`scripts/check-metrics-quality-gate.sh` will inspect the changed paths and run the baseline verifier whenever a commit changes metric-sensitive code.

Initial trigger paths:

```text
packages/core/src/coding_trajectory/ingestion/
packages/core/src/coding_trajectory/metrics/
packages/core/src/coding_trajectory/analysis/
packages/core/src/coding_trajectory/contracts.py
packages/core/src/coding_trajectory/service.py
packages/core/src/coding_trajectory/runtime.py
docs/token-usage-glossary.md
validation/metrics/
```

The repository's agent workflow must run this command before committing a matching change. CI or a local commit hook may call the same script later, but the validation command remains the single source of truth.

Changes outside the trigger paths can run the command explicitly when they alter a plugin projection or public interpretation of core metrics.

## Failure Report

A failed gate reports the smallest useful path to the difference:

```text
case: claude-cache-write-compaction
surface: session.usage
scope: sessions[0].turns[3]
field: usage.cached_prompt_tokens
expected: 51136
actual: 39680
source refs: source/session.jsonl:42, source/session.jsonl:57
audit ref: audit.md#turn-4-provider-usage
```

The report groups failures by case and surface, distinguishes schema failures from value regressions, and retains enough context for an agent to inspect the exact upstream source events.

## Intentional Contract Changes

The validation command must never provide an automatic `--update` or snapshot-blessing mode.

When a metric contract intentionally changes:

1. Document the semantic change and affected fields.
2. Run the old baseline and retain the failure report.
3. Reconstruct the new expected values from source evidence.
4. Update the relevant audit arithmetic and expected JSON.
5. Record the migration in provenance.
6. Obtain a second review.
7. Run the gate cleanly before commit.

This makes a baseline update evidence that the contract changed intentionally rather than a way to hide a regression.

## Baseline Lifecycle

Cases move through explicit states:

```text
candidate -> audited -> active -> superseded or retired
```

Only active cases participate in the required gate. Superseded cases remain available when they document an old provider format that is still useful for migration history. A case is retired only when the corresponding input format is no longer supported or its evidence cannot be retained safely.

## Rollout Plan

### Phase 1: Baseline inventory

- Identify candidate Codex CLI, Claude Code, and Pi historical graphs.
- Build the behavior-coverage matrix.
- Decide which raw logs can be sanitized and committed safely.

### Phase 2: Independent audit

- Produce sanitized evidence bundles.
- Perform source-only reconstruction.
- Resolve discrepancies before creating expected JSON.
- Complete second review and activate the first cases.

### Phase 3: Validator

- Add the Pydantic artifact models.
- Implement the standalone verifier and invariant checks.
- Add terminal and JSON reports.

### Phase 4: Change-aware gate

- Add the path-trigger wrapper.
- Document the required command in the repository agent instructions.
- Add CI invocation later if the repository adopts CI for validation scripts.

## Acceptance Criteria

- At least one active audited case exists for Codex CLI, Claude Code, and Pi.
- The initial cohort covers single-session and multi-session graphs.
- Expected metrics were reconstructed without seeing current `ct` output in the first audit pass.
- The validator detects a deliberate one-token, one-turn, one-status, and one-cost-evidence mutation.
- The verifier produces a field-level source-linked diff.
- Live user log directories and live pricing services are not required.
- No unit-test files or test-runner dependency are introduced.
- Metric-sensitive core changes have one documented command that automatically selects and runs this gate.
