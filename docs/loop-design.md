# Loop guide

Loop is the local-first browser product over Core. Analytics answers “What
happened?” Monitor checks human-configured conditions. Improve is a future
workflow, not an implemented API or placeholder screen.

## Start Loop

Run these commands from the repository root:

```sh
uv sync --all-packages --locked
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

Loop binds to loopback. It always reads local sources, regardless of the default
CLI connection. It does not publish data, delete source logs, or deploy a hosted service.

## Investigate a session

1. Select a project in Explore.
2. Select a session from the paged inventory.
3. Read the summary and chronology.
4. Select a turn or item to inspect its retained evidence.
5. Copy a canonical reference when another reader needs the same evidence.
6. Save the investigation when you need to return to that scope.

An investigation stores a title, canonical references, and view state, not copied
transcripts. A reference can identify a session, turn, item, event, or prepared
view. Deep links do not grant access, resume a harness, or clone a session.
The receiving host needs the corresponding evidence.

Core owns session meaning, ordering, coverage, and metric formulas. Loop composes
Core responses. It does not create a second canonical model.
Incomplete summary coverage remains incomplete even when one selected item has
more retained detail. Unavailable measurements do not become zero.

### Tool mix and context

The Investigation panel groups calls by Core activity concept. It shows call
counts, visible-token estimates, measured durations, shell-routed calls, and failures.
The largest and slowest calls link to their items.

The sequence uses source order. Above 300 calls, it defaults to a per-turn view.
Context composition comes from `session.stats`. Visible tokens are estimates,
not billed provider tokens. Loop does not infer relevance, waste, or productivity.
Hiding the panel prevents its queries until it is shown again.

Colors identify Explore, Change, Command, and Other families. Labels and the legend
remain necessary; color alone must not communicate group identity or status.
Use the existing React/shadcn components and semantic tokens in `web/src/styles.css`.
Keep evidence controls labeled, keyboard accessible, and usable on narrow screens.

## Run a token-budget watch

Monitor implements `ct.loop.turn_token_budget` v1.0.0. A watch selects a
project/session scope, a Core turn-usage measure, a threshold, and finding severity.
It requests no transcript access, external egress, notifications, or enforcement.

1. Select the strategy in Monitor.
2. Configure the watch scope, measure, and threshold.
3. Run a historical dry-run to inspect preview evaluations.
4. Enable the watch when its policy is acceptable.
5. Request refresh to evaluate newly observed local evidence.
6. Inspect evaluations and triage any findings.

Dry-run is bounded by `max_sessions` and does not persist findings.
Refresh requires an enabled watch; disabled watches return HTTP 409.
Refresh is a human-requested poll, not an automatic push or continuous schedule.

Each eligible turn produces an evaluation. Passes, breaches, pending turns,
unavailable evidence, and errors remain distinct. An all-zero usage payload
without request observations is unavailable, not a zero-token pass.
The evaluation states its measure, observed value, threshold, comparison, and coverage.

Configuration changes create a new watch revision. Changed evidence supersedes
an earlier evaluation without deleting its history. A finding has `open`,
`acknowledged`, `resolved`, or `dismissed` triage state. Triage never changes the
underlying evaluation or canonical evidence.

Monitor runs persist progress. After a failed or interrupted run, inspect partial
results before resuming. Resume uses the original parameters and requires the
same watch revision. Only one run can be active for a watch.

## Local state and security

| State | Default path | Override |
| --- | --- | --- |
| Investigations | `~/.coding-trajectory/loop/investigations.sqlite3` | `--state` |
| Monitor configuration, runs, evaluations, and findings | `~/.coding-trajectory/loop/monitor.sqlite3` | `--monitor-state` |

Both stores use owner-only permissions. Monitor stores references, measurements,
thresholds, status, and provenance, not canonical transcript or event bodies.
Saved investigations use `host_local` and revision mode `latest`; an optional
prepared-view hash identifies the selected immutable evidence.

Loop has no standalone multi-user authentication. Host, Origin, and Fetch Metadata
checks reject cross-site browser requests and DNS rebinding. CORS is not enabled.
Use `--allow-host` only behind a separately authenticated trusted proxy.
**Do not expose private local evidence through an unauthenticated tunnel.**

Product routes use `ct.loop.v1`. `/api/core` carries Core requests without method
aliases. `/api/session-browser` provides local paged inventory. Other routes own
investigation state and Monitor strategy, watch, run, evaluation, and finding state.
Core types come from the frozen snapshot; Loop state uses strict Pydantic models.

## Qualification and limits

```sh
uv run python scripts/check-core-protocol.py
bun run --cwd packages/plugins/loop/web check
uv run python scripts/check-loop.py
```

Use `scripts/prepare-loop-demo.py DIRECTORY` for synthetic preview evidence.
Set an isolated `HOME` and `CT_AMP_LOG_DIR` to that directory's Amp journals.
Do not point a demo at private source directories.

Current evidence may be incomplete. Dry-run reports scope truncation when its
session bound is reached. Running turns remain pending until later evidence arrives.
Codex project selection depends on its configured projects and recorded working directories.

Model-based follow-up classification, broader Monitor automation, hosted delivery,
and Improve are deferred. Any future evaluator must declare content and egress
permissions. Improvement work must separate candidate changes, independent checks,
and authorized promotion. None of these future capabilities is implied by a watch.
