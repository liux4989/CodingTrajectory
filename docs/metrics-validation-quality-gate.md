# Metrics validation

Use committed source evidence to check canonical reconstruction and public metrics.
The gate is not a snapshot of current output. Expected values must come from an
independent source audit.

## Run the gate

From the repository root:

```sh
scripts/check-metrics-quality-gate.sh
uv run python scripts/validate-metrics-baselines.py
```

The wrapper selects metric-sensitive changes. With no arguments, it checks the
worktree and branch changes against `origin/main`. The direct command always
runs the full active baseline set. Run both before committing metric-sensitive changes.
See the wrapper for the exact trigger paths.

The validator uses production ingestion, graph assembly, and contract-validated
service calls. It checks source hashes, provenance, expected fields, and
cross-field invariants. Failures identify the case, surface, field, source
references, and audit reference. Unexplained differences cause a nonzero exit.

## Evidence ownership

`validation/metrics/manifest.toml` selects active cases and pinned pricing.
Each case contains sanitized source JSONL, hash provenance, source-linked
expected JSON, and an `audit.md` derivation. These are acceptance evidence,
not disposable benchmark output.

| Active case | Main boundary |
| --- | --- |
| `codex-fork-runtime` | Separate conversation forks, cache/reasoning accounting, runtime |
| `claude-stream-cache` | Repeated response events, cache reads, tool lifecycle |
| `pi-reported-cost` | Provider-reported cost and cached usage |
| `codex-interagent-turn` | Spawned-agent turns, orphan-marker rejection, graph totals |

The gate does not need live user logs, provider APIs, or current pricing.
Cost cases use committed rates or source-reported cost.
Sanitization must preserve every field needed by the asserted behavior.
Rewrite all affected references together when sanitization changes IDs.

## Change a baseline intentionally

**Do not copy current command output into expected values.**

1. Document the semantic change and affected fields.
2. Retain the old gate's failure report.
3. Reconstruct new values from the committed source evidence.
4. Update the audit with arithmetic and exact source references.
5. Update expected JSON to match that audited derivation.
6. Record the migration in provenance.
7. Obtain an independent review.
8. Run the full gate before committing.

The validator has no automatic update or baseline-approval mode.
Review must check derivation and sanitization, not only matching JSON.
The original cohort's implementation cross-check does not establish independent
organizational sign-off; retain that governance distinction.

## Contract rules and limits

- Compare IDs, counts, token integers, statuses, and relationships exactly.
- Compare timestamps after canonical UTC serialization.
- Keep unavailable evidence absent; never replace it with zero.
- Keep root-session measurements, subagent sections, and graph totals distinct.
- Do not present overlapping subagent runtime as user elapsed time.
- Keep usage correctness separate from pricing correctness.
- Treat pinned price changes as intentional baseline changes, not market updates.

The cohort verifies its committed evidence, not every historical provider format.
It uses trajectory retention; it does not establish universal compact/full parity.
Copied-parent-prefix classification, unknown tools, and other unsupported evidence
need source-backed qualification before broader claims.

Keep unit tests out of this repository, as required by [AGENTS.md](../AGENTS.md).
Static analysis, protocol checks, and integration qualification remain separate gates.
See the [token glossary](token-usage-glossary.md) for measurement semantics and the
[benchmark guide](../benchmarks/README.md) for generated reports.
