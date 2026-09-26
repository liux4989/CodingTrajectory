# CodingTrajectory Loop product design

CodingTrajectory Loop (short name **Loop**) is the local-first product layer over
CodingTrajectory Core. It consumes the
complete public Core API and does not mirror, rename, replace, or independently
implement Core's Chronicle, contextual, or metric methods. Core remains the
only owner of canonical session meaning and native metric formulas.

This is a greenfield product and plugin design. Existing pages, methods,
protocol, persistence, and deployment structure mix several jobs and do not
constrain the target. The replacement does not refactor or preserve compatibility
with the existing Datahub plugin.

# Replacement policy

The former Datahub plugin is retired as one implementation unit. Loop is created
from an empty `packages/plugins/loop` boundary with its own `ct.loop.v1` protocol.
It does not run beside the old plugin or inherit its name, protocol, workspace
path, routes, or storage.

The replacement implementation cut must atomically:

1. delete the old Python backend, web application, protocol models, generated
   contracts, derived-store schema, plugin commands, qualifications, and
   Datahub-specific deployment wiring;
2. remove old Datahub entry points from the workspace, CI, release tooling, and
   documentation;
3. create the new plugin package and web entry point from the accepted
   Analytics, Monitor, Improve, and shared-reference contracts;
4. add new checks that load only the new plugin and reject stale old protocol or
   route references.

There is intentionally no:

- old-method adapter or protocol-version negotiation;
- legacy route alias or old frontend shell;
- migration of the old Datahub cache, read models, or derived database;
- feature flag selecting old versus new Datahub;
- requirement to preserve old page URLs or response shapes.

Canonical Core history is not deleted or migrated. The new Analytics product
rebuilds its factual views by calling Core. New Monitor and Improve product state
starts under new schemas because the old plugin has no compatible ownership
model for strategies, evaluations, findings, candidates, checks, or frontiers.

Code may be reused only when it is demonstrably generic and independent of the
old Datahub contracts—for example, an authentication primitive or visual
component. Reuse is a fresh dependency decision, not an obligation to preserve
behavior. Copying old projections, API envelopes, navigation, stores, or page
assemblies into the new plugin is prohibited.

The owner separately approved retirement of the old Cloudflare deployment on
2026-09-17. Both legacy Datahub Workers and their dedicated Access applications
were [deleted](datahub-retirement-2026-09-17.md); the Chronicle control plane was
preserved. Loop currently runs locally. This retirement does not deploy hosted
Loop; a future remote deployment is a separate operation from source replacement.

# Product structure

Loop has three categories with different user questions:

| Category | User question | Evidence boundary |
| --- | --- | --- |
| Analytics | What happened? | Canonical evidence and native descriptive metrics |
| Monitor | What is happening, and did a configured condition match? | Versioned strategy evaluations over arriving Core evidence |
| Improve | How do we turn a finding into a verified improvement? | Bounded, auditable self-improvement iterations and follow-up quality checks |

The categories share references, not copied session models or duplicated
screens. Analytics may open a Monitor evaluation at its exact evidence. A
Monitor finding may start an improvement. Its quality-check result always links
back to the change, evaluations, and canonical facts that support it.

Loop can maintain derived stores for query and strategy execution. They are
rebuildable product state, not canonical compatibility boundaries.

# Shared reference

`CanonicalReference` is the cross-category handoff object. It identifies one
graph, session, turn, item, event, or explicit bounded range and may carry a
source revision. It never embeds credentials or transcript content.

The same reference supports:

- stable Loop deep links;
- exact evidence links from evaluations, findings, and improvements;
- “copy agent reference” for a supported harness adapter;
- progressive resolution from overview to item or event evidence.

Possessing a link does not grant access. Resolution uses the viewer's identity,
Core capabilities, and evidence coverage. A receiving agent remains in its
current session; dereferencing does not resume, fork, or clone the referenced
session. Task-relevance selection by an adapter is disclosed with match reasons
and citations rather than treated as a canonical fact.

# Analytics

Analytics is the factual reading surface. It includes individual session detail
and statistical descriptions, but it does not score productivity, infer quality,
or recommend a change.

## Core object: Investigation

An `Investigation` is temporary or saved view state over a canonical scope. It
contains selected references, filters, ordering, display state, and source
revision. It does not copy the referenced Core records or create a new session
meaning.

## Primary workflow

1. Explore recent sessions or search factual metadata and retained text.
2. Select a graph, session, or narrower canonical scope.
3. Orient with identity, source, coverage, outcome, topology, and native totals.
4. Narrow by branch, turn, item kind, status, time, tool, model, or request.
5. Inspect a synchronized chronology and exact evidence.
6. Copy a stable reference, share permitted evidence, or pass the reference to
   another coding agent.

## Screens

- **Explore** — recent sessions, projects, search, factual filters, source, and
  coverage state.
- **Investigation** — coordinated topology, chronology, native measurements,
  a whole-session tool mix and context composition panel,
  selected-item detail, and evidence drawer. Context and cache facts appear at
  their relevant turn or request rather than as a separate product universe.
- **Reports** — user-selected factual cohorts and descriptive statistical data;
  every aggregate drills down to its contributing sessions.
- **Reference** — a bounded graph/session/turn/item/event/range view optimized
  for human verification and agent dereferencing.

## APIs

Analytics reads Core methods directly. Loop APIs are needed only for saved
investigation state, link delivery, authentication, source selection, and
revision pinning. A server-side display method is justified only when bounded
Core results cannot be composed cheaply in the client; it must not introduce a
parallel canonical or metric contract.

# Monitor

Monitor is a human-directed strategy execution runtime over arriving Core
evidence. Initially a human proposes or approves each strategy's semantics,
scope, condition, permissions, and finding policy. Agents and deterministic
workers execute that approved strategy repeatedly; they do not silently invent
or activate new monitoring policy. “Live” describes when evidence is evaluated;
it does not imply that every result is an alert. The runtime must retain ordinary
successful observations as well as condition matches so that later rates and
coverage can be calculated honestly.

## Product objects

### Strategy

A `Strategy` is a versioned plugin definition. Its manifest declares:

- strategy identity, version, description, and author;
- configuration schema and scope types;
- trigger and required input window;
- evaluator type: deterministic rule, local model, external model, or human;
- output schema and the conditions that may emit findings;
- required Core capabilities and evidence coverage;
- permissions, including content access, external data egress, notifications,
  and any future enforcement action.

Strategy code does not alter canonical records or Core metric formulas.

### Watch

A `Watch` is one configured installation of a strategy. It binds an immutable
strategy version to configuration, project/session scope, execution mode,
permissions, and enabled state. Configuration changes create a new effective
revision so historical results remain reproducible.

### Evaluation

An `Evaluation` records every attempted strategy execution, not only failures.
It contains:

- watch, strategy version, configuration revision, and trigger;
- exact input references and input/source revision;
- pending, completed, unavailable, errored, or superseded execution state;
- typed result, observed values, condition explanation, and evidence links;
- evaluator provenance such as rule version or model/rubric identity;
- coverage and explicit reasons for unavailable or indeterminate results;
- start and completion timestamps.

The runtime is idempotent for the same watch revision, input scope, and source
revision. If mutable live evidence changes, the newer evaluation supersedes the
old result instead of silently rewriting it.

### Finding

A `Finding` is an actionable work item optionally emitted by an evaluation. It
contains severity, expected and observed condition, exact evidence links, and
the lifecycle `open`, `acknowledged`, `resolved`, or `dismissed`.

A finding is not the complete strategy result. Evaluations that pass a limit or
classify an interaction as agreed remain queryable without creating inbox noise.
Resolving or dismissing a finding changes triage state, never its underlying
evaluation or evidence.

## Primary workflow

1. A human proposes or selects a strategy and inspects its declared inputs,
   outputs, and permissions.
2. Configure and approve a watch, scope, condition, and finding policy.
3. Dry-run it against bounded historical evidence and inspect sample evaluations,
   unavailable cases, coverage, and prospective findings.
4. Enable the watch for arriving `living.sessions` and `living.events` changes.
5. Inspect evaluation activity or triage emitted findings.
6. Open exact Analytics evidence, acknowledge or close the finding, or start an
   improvement from selected findings and evaluations.

Dry-run results are labeled and do not create live findings by default.
Initially Monitor only observes and advises. Enforcement requires a separate,
explicit permission and audit design.

## Screens

- **Monitor / Activity** — active watches, recent evaluations, delayed inputs,
  errors, and evidence coverage.
- **Monitor / Findings** — open, acknowledged, resolved, and dismissed findings;
  filters by strategy, project, session, and severity; exact evidence links; and
  explanation of the configured condition.
- **Monitor / Strategies** — available plugins, configuration and scope,
  historical dry-run preview, enable/disable state, and permissions.
- **Strategy detail** — versioned configuration, evaluation history, result
  distribution, coverage, errors, and emitted findings.

## APIs

The product API needs separate operations for the four objects:

- strategy catalog and manifest retrieval;
- watch create/update/list/get, dry-run, enable, and disable;
- evaluation list/get with strategy, scope, state, time, and result filters;
- finding list/get plus acknowledge, resolve, dismiss, and reopen commands.

Evaluation and finding responses carry `CanonicalReference` values. The runtime
consumes Core methods and live change feeds; it does not add Monitor fields to
Core responses.

## Case 1: turn token budget

The strategy configures a maximum native token measurement per turn, project or
session scope, and finding severity. “Token cost” must be made precise in the
configuration: token count is not currency cost. A currency budget may use
provider-reported cost when available; catalog-priced estimates remain labeled
enrichment.

When a turn's required usage evidence becomes available or changes, the strategy
reads the Core-owned turn usage measurement and writes one evaluation containing
the observed value, threshold, comparison, turn reference, and coverage. A
breach emits a finding; a value within budget remains a completed evaluation.
Unavailable or partial usage is recorded as such rather than treated as zero.

This is a deterministic policy evaluation. Core owns the token value and its
formula; the strategy owns the chosen threshold and breach interpretation.

## Case 2: follow-up agreement

The strategy evaluates a three-part input window:

1. a human request;
2. the corresponding completed agent response;
3. the next eligible human follow-up.

It is delayed until the follow-up exists. A versioned LLM rubric classifies the
follow-up as `agreed`, `clarification`, `disagreed`, `new_request`, or
`indeterminate`, with an explanation and exact references to all three inputs.
`new_request` prevents a topic change from being counted as agreement;
`indeterminate` prevents forced certainty. A session with no later follow-up is
reported as pending or censored, not as agreement.

Because this sends transcript content to an evaluator, the watch must declare
content-read and external-model egress permissions. Each evaluation records the
model and rubric version. A human review may append an override while preserving
the original judgment.

The product label “agreed ratio” is an aggregate, not the per-interaction
strategy result. Its default denominator includes only eligible, completed
`agreed`, `clarification`, and `disagreed` evaluations; the UI also shows each
class count, excluded/censored counts, and evidence coverage. The formula and
strategy version are always visible.

The runtime may optionally emit a finding for `disagreed` or repeated
`clarification` results. An aggregate strategy can also emit a finding when the
agreed ratio for an eligible cohort crosses a configured threshold. Improve then
turns that finding into a specific change and checks subsequent interactions.
The LLM judgment remains a labeled interpretation; the request, response, and
follow-up remain the facts.

# Improve

Improve is the mostly autonomous execution loop. Monitor and its human-approved
strategies identify findings. After a human initially admits a finding and
approves the target, acceptance gate, permissions, and budgets, agents may
generate candidates, implement them in isolation, review them, run checks,
reject them, and revise them without approval at every experiment. Promotion to
the active target remains human-gated in the first product phase.

The final goal is bounded full recursive self-improvement (RSI): a promoted
candidate may change the Monitor or Improve harness that performs the next
cycle, and policy may authorize subsequent finding selection and promotion.
Recursion does not remove immutable evidence, independent gates, budgets,
rollback, or stop conditions. Improve is not an unbounded agent that silently
rewrites itself.

## Product objects

### Improvement

An `Improvement` is one lineage attempting to resolve one or more related
findings. It contains:

- one or more source findings and their supporting evaluations and evidence;
- the concrete improvement target, such as an instruction, skill, strategy
  configuration, model policy, or workflow component;
- the objective, protected capability floors, and expected effect;
- approved mutation scope, evaluator authority, budgets, and stop conditions;
- an ordered lineage of candidates, checks, and dispositions;
- the current verified frontier and any promoted change;
- an optional parent improvement when a follow-up finding starts a new lineage.

Its lifecycle is `draft`, `awaiting_approval`, `researching`,
`awaiting_promotion`, `monitoring`, `passed`, `failed`, `inconclusive`,
`reverted`, or `cancelled`.

### Candidate

A `Candidate` is one immutable proposed version in an improvement lineage. It
records its parent candidate, rationale, exact patch or configuration change,
target revision, generating agent and tools, expected effect, and disposition.
Candidate dispositions include `revise`, `rejected_review`, `rejected_trial`,
`rejected_held_out`, `promotion_ready`, and `superseded`. Rejected candidates
remain durable negative evidence rather than disappearing after rollback.

Candidate changes may be applied automatically only to an isolated trial target
within the approved mutation scope. This trial application is not promotion to
the active target.

### QualityCheck

A `QualityCheck` is an executable, versioned gate. Its kinds are:

- **review** — independent inspection of implementation and behavioral contract;
- **trial** — checks against development or historical evidence;
- **held-out** — final pre-promotion check against evidence not used for
  candidate generation or revision;
- **follow-up** — post-promotion check over newly arriving evidence.

Every check defines and records:

- locked evaluator strategy and version;
- baseline cohort and values;
- observation scope, eligibility rules, and minimum sample;
- protected capability floors as well as the objective to improve;
- success, failure, and inconclusive conditions;
- evaluations and coverage;
- `passed`, `failed`, or `inconclusive` result with evidence links.

The evaluator may be deterministic or model-based and retains the same
provenance and permission requirements as a Monitor strategy. The check never
changes its criterion after seeing the result. Candidate, evaluator, acceptance
gate, and held-out evidence manifest are frozen together before the held-out
check. Held-out results do not become revision feedback for that candidate.

### AppliedChange and VerifiedFrontier

An `AppliedChange` is the permissioned promotion of a candidate to the active
target. It records before and after revisions, candidate, actor, approval or
authorizing policy, timestamp, rollout scope, and rollback capability. If
Loop cannot promote the target directly, it records an external change
reference and waits for confirmation of the resulting revision.

`VerifiedFrontier` identifies the current accepted candidate for a target. It
advances only after the required pre-promotion gates pass and promotion is
authorized. A follow-up quality check may retain it, trigger rollback, or emit a
new finding; it never erases prior frontier or candidate history.

## Primary workflow

1. A human triages a Monitor finding and inspects its evaluations and evidence.
2. The human admits it into Improve, selects one concrete target, and approves
   mutation scope, capability floors, objective, evaluator authority, budgets,
   and stop conditions.
3. Improve automatically establishes a baseline and generates Candidate v1.
4. An implementation agent applies the candidate to an isolated trial target;
   an independent reviewer and trial checks evaluate it.
5. Failed candidates remain in the lineage. Within budget, Improve may revise
   the implementation, create another candidate, or gather more evidence.
6. Improve freezes a surviving candidate and its acceptance gate, then runs the
   held-out check. Failure terminally rejects that frozen candidate rather than
   leaking held-out results back into its revision loop.
7. A passing candidate becomes promotion-ready. Initially a human approves its
   `AppliedChange`; later an explicit policy may authorize low-risk promotion.
8. The promoted candidate advances the target's `VerifiedFrontier`. Monitor
   collects new eligible evidence for the follow-up quality check.
9. On pass, resolve the source finding. On failure, rollback when authorized and
   emit a follow-up finding. Inconclusive evidence keeps the result explicit and
   may request a larger observation window.

The recursive edge exists when the promoted target influences the next Monitor
or Improve cycle. Every cycle remains finite and independently auditable.

## Screens

- **Improve / Queue** — findings ready for improvement, active iterations,
  checks waiting for evidence, and completed outcomes.
- **Improve / Improvement** — source finding, target, proposed and applied
  candidates, frontier, approval, quality-check progress, and lineage.
- **Improve / Candidate** — immutable change, parent candidate, rationale,
  implementation record, reviews, checks, disposition, and exact artifacts.
- **Improve / Quality check** — locked criteria, baseline and post-change
  cohorts, evaluator provenance, coverage, and exact supporting and
  contradictory evidence.
- **Improve / Approvals** — changes and reversions requiring human permission.

## APIs

Improve APIs own improvement create/list/get; candidate list/get and generation;
check definition, execution, and disposition; promotion approval, apply, revert,
and cancel commands; frontier retrieval; and follow-up finding creation.
Quality-check observations reuse the strategy evaluation contract. Improve
references Core and Monitor records rather than copying them, and it cannot
resolve a source finding until the locked follow-up check passes or a human
records an explicit override with rationale.

## Example: improving follow-up agreement

1. The follow-up agreement strategy classifies eligible interactions.
2. An aggregate strategy emits a finding when the agreed ratio falls below a
   configured threshold for a sufficiently covered cohort.
3. A human admits the finding, selects a versioned agent instruction as the
   target, and approves the mutation scope and budgets.
4. Improve generates and tests candidate instruction revisions automatically.
   The gate protects correctness and evidence quality while requiring an agreed
   ratio improvement under the locked rubric.
5. A surviving candidate and gate are frozen and checked on held-out evidence.
6. After promotion approval, new interactions provide the follow-up check. It
   passes, fails, or remains inconclusive based on the locked rules.
7. A failed check can roll back and create a follow-up finding and lineage; it
   does not silently promote another instruction revision.

# Reference patterns

The improvement vocabulary deliberately combines two reference designs:

- [Karpathy autoresearch](https://github.com/karpathy/autoresearch) contributes
  baseline, immutable candidate, incumbent/frontier, bounded experiment, and
  explicit advance/discard/crash dispositions. Loop retains rejected
  candidates durably and does not adopt its single-metric, greedy, globally
  unbounded loop.
- [NVIDIA SoL-Pi](https://nvlabs.github.io/SoL-Pi/) contributes evidence-driven
  proposal lineages, independent implementation review, capability floors,
  objective improvement, frozen candidates and gates, isolated held-out checks,
  evidence receipts, and verified frontiers. Loop does not assume that one
  verifier proves general quality or that independently safe mechanisms compose
  without a combined check.

Both reinforce the same authority rule: the agent proposing or implementing a
candidate does not unilaterally redefine its evaluator or promote itself.

# Autonomy roadmap

| Phase | Monitor | Improve | Promotion |
| --- | --- | --- | --- |
| Human-in-the-loop (HIL) initial | Human proposes and approves strategies and watches; agents execute | Human admits a finding and sets policy; agents autonomously research candidates within it | Human approval required |
| Policy automation | Agents may propose strategies, but humans approve activation | Findings matching policy may start automatically; candidate research remains bounded | Pre-authorized low-risk targets may promote automatically |
| Full bounded RSI | The system may improve and activate its own monitoring strategies within policy | The verified Improve harness may generate its successor and run the next lineage | Policy-authorized promotion with enforced rollback, budgets, and stop conditions |

At every phase, agent proposals and executions are distinct records. Increasing
autonomy changes who may issue commands; it does not weaken evidence,
provenance, held-out isolation, capability floors, or audit requirements.

# Layer responsibilities

| Layer | Owns | Does not own |
| --- | --- | --- |
| Core Chronicle | Canonical resources, hierarchy, evidence references, normalization, ordering, and provenance | Loop product categories or strategy judgments |
| Core metrics | Native scoped measurements and their formulas | User thresholds, scores, or recommendations |
| Core display | Reference overview, summary, and search projections over Chronicle | Privileged facts unavailable to other consumers |
| Loop delivery | Browser authentication, source connection, revision state, and bounded delivery | A parallel Core method registry |
| Analytics | Investigation state and factual presentation | New canonical resources or metric formulas |
| Monitor | Strategies, watches, evaluations, and findings | Rewriting facts or presenting judgments as facts |
| Improve | Improvement lineages, candidates, checks, verified frontiers, promotions, and rollback | Silent or unbounded self-modification |

# Initial product cut

The first coherent release should make Analytics investigation and portable
canonical references excellent, then add the strategy runtime with the turn
token-budget strategy. Follow-up agreement is a suitable first interpretive
strategy once content permissions, delayed windows, evaluator provenance, and
human override are implemented. Improve begins with one manually approved
finding and policy envelope, followed by autonomous candidate research and a
human-approved promotion. Policy-authorized promotion and full recursion are
later phases.

Defer generic dashboards, developer productivity or efficiency rankings,
production SLO enforcement, prompt hosting, uncited semantic memory, and
automatic recommendations. Existing Today, Compare, Code Time, and Efficiency
pages are not target IA commitments; implementation reuse is evaluated only
after these product contracts are accepted.

# Acceptance boundary

The greenfield design is coherent when:

1. The replacement contains no old Datahub protocol, route, store, projection,
   compatibility adapter, or legacy-mode dependency.
2. Analytics, Monitor, and Improve answer distinct user questions.
3. Every aggregate or judgment drills down through stable references to Core
   evidence.
4. Strategies retain every evaluation needed for honest rates and coverage,
   while findings remain optional actionable work items.
5. Deterministic rules and model judgments declare different evaluator and
   permission semantics.
6. Cross-category transitions pass references rather than duplicate data or
   screens.
7. Every improvement retains immutable candidate lineage, including rejected
   experiments and their evidence.
8. Candidate, evaluator, gate, and held-out manifest are frozen before final
   pre-promotion evaluation.
9. Promotion advances an explicit verified frontier and remains distinct from
   isolated candidate application.
10. Failed and inconclusive follow-up checks create explicit work or rollback
   rather than triggering hidden recursive changes.
11. No Loop method duplicates a Core method or native metric formula.

# Implemented local slice

The implementation ships Explore, Investigation, and exact evidence (Analytics),
plus the deterministic Monitor foundation: the first-party turn-token-budget
strategy with watch configuration, historical dry-run, manual refresh over the
local `living.sessions` change feed, evaluations for every eligible turn, and
finding triage. Improve objects remain design boundaries, not implemented APIs
or placeholder screens. Upload, publication, hosted delivery, Chronicle preview
policy, and compression are deferred. Source removal does not alter deployed data.

Run from the repository root:

```bash
uv sync --all-packages --locked
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

Loop binds only to loopback. The server serves the built React application and:

| Route | Owner and behavior |
| --- | --- |
| `POST /api/core` | Local transport to `ServiceRuntime.execute({method, params})`; forwards Core's runtime result and source metadata unchanged; no method aliases, copied resource schemas, or formula implementations |
| `GET /api/status` | `ct.loop.v1` local connection status; no remote fallback |
| `GET /api/investigations` | `ct.loop.v1` latest 100 saved views, newest creation first |
| `POST /api/investigations` | `ct.loop.v1` create/update by UUID; strict Pydantic reference-only view state |
| `GET /api/monitor/strategies` | `ct.loop.v1` first-party strategy catalog: versioned manifest, config schema, triggers, permissions, and required Core methods |
| `GET/POST /api/monitor/watches` | `ct.loop.v1` watch list/create; strict Pydantic scope and config validation |
| `GET/POST /api/monitor/watches/{id}` | `ct.loop.v1` watch get/update; scope or config changes append a new revision and reset the refresh position |
| `POST /api/monitor/watches/{id}/dry-run` | Bounded labeled historical evaluation over existing local Core evidence; never persists findings |
| `POST /api/monitor/watches/{id}/refresh` | Enabled-gated evaluation of newly observed sessions from the local change feed; 409 while the watch is disabled |
| `GET /api/monitor/evaluations` | `ct.loop.v1` evaluation list with watch, trigger, state, and result filters |
| `GET /api/monitor/findings` | `ct.loop.v1` finding list with status and watch filters |
| `POST /api/monitor/findings/{id}/status` | `ct.loop.v1` open/acknowledged/resolved/dismissed transitions with durable history |

Each query uses a fresh local runtime so its in-memory cache does not freeze
changing logs. Core still owns local canonical-repository selection, source
discovery, ordering, coverage, and any unsupported-query errors. The transport
uses Core's in-process runtime envelope (`result`, `ok`, `meta`), not a second
public Core envelope definition. Browser types are generated from the frozen
Core snapshot and Loop's Pydantic models. `bun run check` detects type drift.

Investigation SQLite defaults to `~/.coding-trajectory/loop/investigations.sqlite3`
with owner-only permissions. `--state` selects another local path. It stores a
UUID, user title, canonical session/turn/item/event IDs, source `host_local`, and
revision `latest`. It does not persist canonical records, metrics, or transcript
content. Views are not revision-pinned. Deep links encode those IDs in the URL
fragment, and copied references include `ct.loop.v1`, source, and revision mode.
The same local source must be available to resolve a link. No harness is resumed.

The browser reads summary and up to five recent overview turns first, then loads
20 canonical items per request and resolves explicit item/event selections.
Metadata and content coverage stay distinct; a partial summary is not relabeled
complete because one local item has full content. Core provenance and exact
ordering remain available in the evidence panel. No status or missing count is
converted to zero.

## Monitor implementation notes

Monitor persists to a separate SQLite store, defaulting to
`~/.coding-trajectory/loop/monitor.sqlite3` with owner-only permissions;
`--monitor-state` selects another local path. It holds three tables — `watches`,
`evaluations`, `findings` — and stores only references, measured values,
thresholds, configuration, status, and provenance. Canonical transcript or event
bodies are never copied; every evaluation and finding carries the exact
`session_id`/`turn_id` reference back to Core evidence, which Analytics resolves.

The turn-token-budget strategy is deterministic (`ct.loop.turn_token_budget`
v1.0.0). The human configures project/session scope, one exact Core-owned
per-turn native measure (the nine `session.usage` turn-usage keys), a token
threshold, severity, and whether breaches emit findings. Every eligible turn
produces an idempotent evaluation keyed by watch, revision, turn, trigger, and
evidence fingerprint — passes, unavailable, pending, and errors included.
Unavailable evidence is never treated as zero: an ended turn whose only usage
payload is all-zero and whose request ledger holds no observations is recorded
`unavailable` with the reason, not `pass(0)`. When later evidence changes an
already-evaluated turn, the new evaluation supersedes the old one in place; the
old record keeps its `superseded_by` pointer instead of being rewritten.

Historical dry-run is bounded by project/session scope and `max_sessions`;
results are labeled `historical_dry_run` and prospective findings are previewed
but never persisted. Refresh consumes the local `living.sessions` change feed
with a per-watch continuation cursor and watermark, and stays disabled (HTTP
409) until a human explicitly enables the watch. Refresh is a human-requested
poll, not a live push guarantee: evaluations appear when someone asks for them.

Monitor's Core coupling is isolated to five frozen methods — `project.list`,
`project.sessions`, `session.usage`, `session.request_usage`, and
`living.sessions` — called through one local ServiceRuntime facade per run
(local repository, no remote fallback). The plugin manifest
(`packages/plugins/loop/plugin.toml`) pins their current contract versions.
Monitor reads token measurements; it does not reimplement Core's metric
formulas, and no Loop route mutates canonical records.

Known honest limitations of this slice:

- Core project/session inventory is unpaginated (the accepted non-blocking
  challenge below). Dry-run bounds work with `max_sessions` and reports
  `scope_sessions_total`/`truncated` when the bound cuts the scope.
- Codex project-scope matching depends on frozen Core inventory behavior:
  project names come from `~/.codex/config.toml` `[projects]` entries and
  recorded session working directories, and project-scoped Codex queries require
  the server's working directory to be tracked there.
- A turn still running when evaluated is recorded `pending`, and an evaluation
  reflects the evidence present at evaluation time; re-running refresh after
  more evidence arrives supersedes it.

Security boundary: a local user owns access to this service. It has no standalone
multi-user authentication. Host, Origin, and Fetch Metadata checks reject DNS
rebinding and cross-site browser requests; no CORS is enabled. `--allow-host` is
only for a separately authenticated trusted proxy, never direct network exposure.
Raw local evidence must not be exposed through an unauthenticated tunnel.

Qualification:

```bash
uv run python scripts/check-core-protocol.py
uv run python scripts/check-loop.py
bun run --cwd packages/plugins/loop/web check
```

`scripts/prepare-loop-demo.py DIRECTORY` creates explicitly synthetic Amp evidence
for isolated preview. Set `HOME` to that directory and `CT_AMP_LOG_DIR` to its
`.coding-trajectory/amp/sessions`, and launch Loop with the workspace Python.
Never point a demo preview at a user's real source directories.

## Non-blocking Core challenge

`project.list` and `project.sessions` have no frozen limit/cursor request fields.
Explore therefore displays unpaginated inventory honestly and offers project
scoping and a client-side metadata filter, not pretend server pagination.
A large-inventory workflow needs a separately reviewed bounded inventory
contract with stable ordering and continuation. This slice does not change Core.

## Next Monitor strategy: follow-up agreement (design only)

With the deterministic foundation verified, the next strategy is the
interpretive follow-up agreement classifier from Case 2 above. It is not
implemented in this slice; the exact design against the shipped foundation is:

- **Manifest.** `follow-up-agreement` v1.0.0, evaluator type `local_model` or
  `external_model` (not `deterministic`), input window "one human request, the
  completed agent response, and the next eligible human follow-up", finding
  policy emitting on `disagreed` or repeated `clarification` when enabled.
- **Permissions.** Unlike turn-token-budget, this strategy must read transcript
  content. Its manifest must declare `content_read` with the exact item kinds,
  and `external_egress` naming the model endpoint, or run a local model with
  egress still declared "Not requested". The watch UI must render these as an
  explicit approval step before enable, reusing the existing permission panel.
  Until that approval UX and an evaluator egress policy exist, the strategy
  must not ship.
- **Window resolution.** Purely reference-based discovery over frozen Core:
  `session.overview` or `session.items` to pair each user-request turn with its
  agent response and the next user turn. Turns without a follow-up yet are
  `pending` (censored), never counted as agreement.
- **Evaluation records.** Same `Evaluation` contract; `condition` carries the
  rubric class (`agreed`, `clarification`, `disagreed`, `new_request`,
  `indeterminate`), the rubric/model version as evaluator provenance, and exact
  references to all three inputs. Persistence stays reference-only: the rubric
  verdict and rationale summary are stored, never transcript bodies.
- **Deliberately not built now.** The deterministic phase grants no
  content-read or egress permission, and wiring one without an approved
  permission/egress design would violate the evidence boundary this slice
  establishes. Implementation starts only after that design is accepted.
