# TypeSafe (Jev) judgment experiments over session evidence, 2026-09-19

**System One judgments are strong enough to propose two Loop-layer features now
(semantic session reranking and natural-language read routing), one Monitor
feature behind dry-run calibration (finding triage), and one to defer (session
intent labels).** Routing went 14/14 including out-of-scope rejection; per-candidate
Noul reranking put every hand-labeled relevant session in the top 5 on both test
queries; finding severity ordered all seven seeded scenarios consistently with
expectations. Intent classification reached only 13/16, with all misses on one
taxonomy boundary — but misses carried measurably lower confidence (0.70 vs 0.92),
so confidence-gating is viable wherever a wrong label is cheap to show.

This is disposable experiment tooling, not a product integration. No runtime
edits, no changes to Core contracts, no new unit tests, no deployments or pushes.
[Aggregate evidence](typesafe-judgment-experiments-2026-09-19.json) contains every
answer, probability distribution, confidence, latency, and token count.

## Setup and attribution

- HEAD at experiment time: `5a249915820f9f6917d6b32962611eebd45d6af1`.
- Harness: `scripts/experiment-typesafe-judgments.py`, run as
  `uv run --with typesafe-sdk python scripts/experiment-typesafe-judgments.py`.
  The SDK is resolved ephemerally; `pyproject.toml`/`uv.lock` are unchanged.
- Model: `jev-latest` alias, resolved to `jev-1.13.0`. API key read from the
  gitignored project `.env`; no key material is committed.
- Python 3.12.11, arm64 macOS. 63 API calls total, sequential, no retries needed.
- Ground truth for experiments 1 and 4 is hand-assigned from real local session
  titles (coding-trajectory project, 204 sessions listed via `project.sessions`).
  Small samples; treat numbers as directional, not qualified baselines.

## Experiment 1: session intent classification (Choice, 8 options)

16 real opening user messages, classified into bugfix / feature / refactor /
ui_change / removal / investigation / docs / ops.

- **Accuracy 13/16 (0.81).** Median latency 386 ms, ~520 input tokens per call.
- All 3 misses were the same boundary: UI-directed work labeled `feature` by
  ground truth was predicted `ui_change` ("import motion to enrich the UX",
  "build a design token"), and one `ui_change` ground truth predicted `feature`.
- **Confidence separates misses from hits:** mean 0.70 on incorrect vs 0.92 on
  correct. A ~0.8 display threshold would have shown all 13 correct labels and
  suppressed 2 of 3 errors.

## Experiment 2: Monitor finding triage (Score + Noul in one call)

7 seeded findings over synthetic Monitor evidence, scored 0–3 severity plus an
interrupt-the-human Noul:

| Case | Expected band | Severity | Interrupt p |
| --- | --- | --- | --- |
| normal_progress | 0 | 0.25 | 0.08 |
| idle_after_completion | 0 | 0.42 | 0.18 |
| budget_92pct_midtask | 1 | 2.02 | 0.50 |
| cost_spike_single_request | 1 | 1.99 | 0.56 |
| repeated_failing_test_loop | 2 | 2.43 | 0.67 |
| budget_exceeded_no_progress | 2 | 2.44 | 0.63 |
| secret_file_leaked | 3 | 3.00 | 0.93 |

- **Ordering is perfectly rank-consistent** with expected bands; the sensitive-
  data case saturated severity (3.00, confidence 1.00).
- Mid-severity compresses (bands 1–2 land at ~2.0–2.4), so severity thresholds
  must be tuned on real findings, not assumed from level indices.
- Interrupt probability separates cleanly: ≤0.18 for no-action cases,
  ≥0.63 for actionable ones, with 0.50–0.56 on genuine boundary cases.

## Experiment 3: natural-language read routing (Choice, 13 options)

12 natural-language asks routed across the 12 core read methods
(`session.summary`, `session.search`, `session.usage`, `project.sessions`, …),
plus 2 out-of-scope requests with a `none_of_these` option.

- **Accuracy 14/14**, median latency 469 ms. Runner-up probability was ≤0.10 on
  all in-scope queries.
- The weakest result was correct and honestly uncertain: "Rewrite this function
  to be async" → `none_of_these` at confidence 0.49 (runner-up `session.items`
  0.29). Confidence flags exactly the cases to escalate.

## Experiment 4: session search rerank (two methods compared)

Two queries over 12-candidate pools of real session titles, 5 and 3 hand-labeled
relevant, against a fixed baseline order:

| Query | Baseline rel-in-top5 | Choice-over-IDs (1 call) | Per-candidate Noul (12 calls) |
| --- | --- | --- | --- |
| redesigning the dashboard web UI | 2/5 | 4/5 | **5/5** |
| diagnosing token usage measurement problems | 3/5 | 3/5 | **3/5** (near-miss at 0.90) |

- **Per-candidate Noul is the better reranker:** perfect top-5 on both queries
  with clean separation (relevant ≥0.76, clear distractors ≤0.41).
- Choice-over-IDs is 5× cheaper (one ~900-token call vs ~4.5k tokens) and fine
  for top-1, but its tail probabilities collapse to 0.0 — on query A a relevant
  candidate ranked 6th with probability 0.0. **Do not use Choice probabilities
  as a full ranking.**
- Noul's one false positive was the deliberate near-miss ("breakdown codex
  memory and skills token cost", 0.90 for "measurement problems") — a genuinely
  debatable label, not a clear model error.

## Proposal

Placement follows the existing layering rule: these judgments are
enrichment-specific interpretations, so they belong in the Loop plugin layer,
never in Core canonical models. All four are opt-in, keyed off an operator-
provided `TYPESAFE_API_KEY`, and cacheable by (question text, evidence content
hash) alongside the prepared-graph cache.

1. **Semantic rerank for session search and session lists (strongest evidence).**
   Add an optional rerank stage after the deterministic structural shortlist:
   per-candidate Noul relevance over title/preview, surfaced when p ≥ ~0.5.
   Cache judgments; 12 candidates ≈ 4.5k tokens and ~5 s sequential, so also
   cap the shortlist and consider the async client for fan-out.
2. **Natural-language routing to core reads.** Front the Loop investigation
   input (and/or a `ct ask`-style entry) with a 13-option Choice over the
   existing read methods, exactly as experimented. Act only at high confidence;
   on low confidence or `none_of_these`, fall back to the current manual
   navigation. The observed confidence behavior (1.0 wrong never happened;
   0.49 flagged the one genuinely ambiguous case) supports a single threshold
   in the 0.6–0.8 band, to be fixed on a larger query set.
3. **Monitor finding triage, dry-run first.** Attach the severity-Score +
   interrupt-Noul pair to evaluation output as advisory fields (no behavior
   change), collect them against human triage decisions, and only then set
   interruption thresholds. The mid-severity compression seen here is exactly
   what the dry-run phase exists to measure.
4. **Defer session intent labels.** 81% accuracy is not card-worthy yet. If
   revisited, merge or redefine the `feature`/`ui_change` boundary in the
   taxonomy and gate display at confidence ≥ ~0.8.

Explicitly not proposed: baking any judgment into Core reads or canonical
resources (layering), and Choice-over-candidates as a ranking mechanism
(tail collapse observed in experiment 4).
