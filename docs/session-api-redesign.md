# Session Query API Redesign

## Status

Implemented additive design. `session.summary.v1` and `session.search.v1` are
registered service contracts and CLI commands. Existing v2 contracts remain
compatible; this is not an API-wide v3 migration.

## Purpose

The session API should let a human or coding agent move from orientation to
exact evidence without reading a full vendor log. The current API already
provides a structural overview and exact item/event lookup, but it lacks:

- a concise interpretation of the session's objective and outcome;
- content discovery when the caller does not already know an item or event ID;
- one explicit vocabulary for summary, overview, search, and detail;
- common rules for evidence references, coverage, and bounded output.

The redesign adds `session.summary.v1` and `session.search.v1` while preserving
the existing v2 methods. It adapts two useful ideas from
[`sting8k/pi-vcc`](https://github.com/sting8k/pi-vcc): deterministic bounded
session briefs and layered recall. CodingTrajectory retains its own stronger
boundaries: canonical UUID references, cross-vendor resources, explicit
session/graph separation, and rebuildable derived projections.

## Decisions

1. **Do a targeted cleanup, not a blocking rewrite.** Define the method
   taxonomy and new contracts first. Existing v2 methods remain compatible.
2. **Keep methods purpose-specific.** Do not add
   `session.get(mode=summary|overview|details)`. Its response would be
   polymorphic and its modes would mix interpretation, structure, and evidence.
3. **`session.summary` means trajectory summary, not inventory metadata.**
   `project.sessions` already owns session discovery cards and counts.
4. **`session.overview` remains sequence-first.** It shows turns and grouped
   activity without claiming to determine the session outcome.
5. **Items and events remain the detail layer.** There is no aggregate
   `session.details` payload.
6. **Search returns references, not replacement evidence.** A match contains a
   bounded snippet and canonical IDs. Callers use `session.items` or
   `session.events` to retrieve the exact evidence.
7. **Session methods never silently widen scope.** `session.*` reads one
   canonical thread. Future orchestration-run and project search use
   `graph.search` and `project.search` with the same match model.
8. **Derived text never becomes a canonical fact.** Summary sections, ranking,
   snippets, and search scores remain rebuildable analysis projections.

## Method Taxonomy

| Family | Question | Methods | Output character |
|---|---|---|---|
| Inventory | What work exists? | `project.list`, `project.sessions`, `session.tree` | Small identity and topology records |
| Interpretation | What was attempted, changed, and resolved? | `session.summary` | Bounded evidence-backed claims |
| Structure | In what order did the work happen? | `session.overview`, `graph.overview` | Turns and grouped activity |
| Discovery | Where is evidence about this subject? | `session.search`; later `graph.search`, `project.search` | Ranked references and snippets |
| Evidence | What exactly was observed? | `session.items`, `session.events` | Reconstructed items and source facts |
| Measurements | What resources did the work consume? | `session.stats`, `session.usage`, specialized usage methods | Accounting and diagnostics |
| Live protocol | What canonical resources changed? | `living.sessions`, `living.events` | Revisioned snapshot/delta changes |

`view` and `details` remain modes of the `living.events` change-feed protocol.
They are not general session information levels.

## Reading Flow

```text
project.sessions
  -> session.summary
       -> session.overview       chronological orientation
       -> session.search         content discovery
            -> session.items     reconstructed action evidence
            -> session.events    source-level event evidence
       -> session.stats/usage    measurements
```

A caller may skip any layer when it already has the required canonical ID.

## Shared Contract Rules

### Scope

New `session.*` methods require a canonical `session_id`. They do not accept a
graph root as an alias and do not fall back to the oldest or root session. An
optional `turn_id` narrows a query and must belong to that session.

This is intentionally stricter than the historical `SessionEntryRequest`,
which accepts session, root-session, or turn entry points. Existing v2 methods
retain that behavior until a separately justified version migration.

### Evidence references

Every derived claim or search match carries the narrowest available canonical
references:

```json
{
  "session_id": "uuid",
  "turn_id": "uuid-or-null",
  "item_id": "uuid-or-null",
  "event_ids": ["uuid"]
}
```

References identify support; snippets are previews and never evidence
authorities.

### Bounds and truthful incompleteness

Every bounded search collection reports:

- `total`: matching entries before the response limit when known;
- `truncated`: whether entries were omitted;
- `coverage`: which retained or hydrated sources were searched/projected;
- `warnings`: corruption, unavailable content, or incomplete reconstruction.

A summary reports the same information for all bounded sections in one
top-level `truncation` map, avoiding a pagination envelope around every small
section.

Missing facts remain absent or `null`. They are never inferred solely to fill a
summary section.

### Projection provenance

Derived responses identify their algorithm:

```json
{
  "projection": {
    "name": "session_summary",
    "version": 1,
    "strategy": "deterministic_structural"
  }
}
```

Changing section semantics, ranking features, or evidence-selection rules
requires a projection-version change. A service method version changes only
when its public request or response contract becomes incompatible.

## `session.summary.v1`

### Semantics

`session.summary` is conclusion-first and bounded. It answers:

- What did the user ask for?
- What materially changed?
- What decisions were recorded?
- What verification was observed?
- What failed or remains unresolved?
- What explicit next action remains, if any?

It is not an LLM-generated retrospective, an evaluation result, or a claim that
the requested outcome is correct. Session liveness and canonical turn status
remain distinct from outcome interpretation.

### Request

```json
{
  "session_id": "uuid",
  "turn_id": "optional-uuid"
}
```

`turn_id` produces the same summary contract for one turn. No section-selection
or arbitrary token-budget options are included in v1; one stable default makes
the projection comparable and benchmarkable.

### Response

```json
{
  "session_id": "uuid",
  "selected_turn_id": null,
  "latest_turn_status": "completed",
  "objective": {
    "text": "Implement incremental session ingestion",
    "references": {
      "session_id": "uuid",
      "turn_id": "uuid",
      "item_id": null,
      "event_ids": ["uuid"]
    }
  },
  "decisions": [
    {
      "text": "Use a revisioned SQLite projection",
      "references": {
        "session_id": "uuid",
        "turn_id": "uuid",
        "item_id": "uuid",
        "event_ids": ["uuid"]
      }
    }
  ],
  "changes": [
    {
      "path": "packages/core/src/coding_trajectory/living_events.py",
      "operations": ["edit"],
      "references": {
        "session_id": "uuid",
        "turn_id": "uuid",
        "item_id": "uuid",
        "event_ids": ["uuid", "uuid"]
      }
    }
  ],
  "verification": [
    {
      "label": "uv run ruff check .",
      "status": "succeeded",
      "references": {
        "session_id": "uuid",
        "turn_id": "uuid",
        "item_id": "uuid",
        "event_ids": ["uuid"]
      }
    }
  ],
  "unresolved": [],
  "next_actions": [],
  "recent_activity": [],
  "truncation": {
    "decisions": {"total": 1, "truncated": false},
    "changes": {"total": 1, "truncated": false},
    "verification": {"total": 1, "truncated": false},
    "unresolved": {"total": 0, "truncated": false},
    "next_actions": {"total": 0, "truncated": false},
    "recent_activity": {"total": 0, "truncated": false}
  },
  "projection": {
    "name": "session_summary",
    "version": 1,
    "strategy": "deterministic"
  },
  "coverage": {
    "retention": "trajectory",
    "content_complete": true
  },
  "warnings": []
}
```

### Section rules

| Section | Merge/selection behavior | Authority |
|---|---|---|
| `objective` | Latest material user request, retaining the initial request when scope did not change | User-message evidence |
| `decisions` | Durable, deduplicated, capped | Explicit observable assistant/user statements |
| `changes` | Merge by normalized artifact path | Successful canonical file-change items |
| `verification` | Keep command and observed status separately | Canonical command/tool outcomes |
| `unresolved` | Volatile; include currently uncorrected failures only | Failed/incomplete evidence, never text sentiment alone |
| `next_actions` | Explicit only; do not invent likely work | User or assistant statements with references |
| `recent_activity` | Small chronological tail after higher-value sections | Canonical activity cells |

Goals, decisions, and changes are sticky within the projection. Unresolved
state is volatile. Recent activity is a rolling bounded tail. Selection may use
structural signals such as mutations, failed validation, commits, user scope
changes, and recency, but final entries retain chronological order within each
section.

### Non-goals

- Scoring whether the session succeeded.
- Replacing the evaluation subsystem.
- Inferring repository state after the recorded session.
- Persisting summary claims in `Session`, `Turn`, `Item`, or `Event`.
- Using private reasoning as evidence.

## `session.search.v1`

### Semantics

`session.search` discovers canonical evidence in one session from text or an
artifact path. It searches retained canonical content and compact retained
previews. It does not search derived
summaries, private reasoning, or its own retrieval commands, preventing
retrieval output from recursively dominating later results.

### Request

```json
{
  "session_id": "uuid",
  "query": "incremental sqlite migration",
  "turn_id": null,
  "mode": "text",
  "kinds": ["user_message", "assistant_message", "tool_call", "tool_result", "file_change"],
  "limit": 20
}
```

Contract rules:

- `query` is required, normalized, and bounded in length.
- `mode` is `text` or `path` in v1. Regex is deferred until timeout and unsafe
  pattern behavior can be enforced across supported runtimes.
- `kinds` defaults to every searchable canonical kind.
- `limit` defaults to 20 and is capped at 50.
- `turn_id`, when present, narrows results and must belong to `session_id`.

### Response

```json
{
  "session_id": "uuid",
  "selected_turn_id": null,
  "query": {
    "text": "incremental sqlite migration",
    "mode": "text",
    "kinds": ["user_message", "assistant_message", "tool_call", "tool_result", "file_change"]
  },
  "matches": [
    {
      "rank": 1,
      "score": 7.42,
      "kind": "file_change",
      "timestamp": "2026-08-01T10:00:00Z",
      "label": "Edited living_events.py",
      "snippet": "revisioned SQLite resource store...",
      "matched_fields": ["path", "tool_input"],
      "references": {
        "session_id": "uuid",
        "turn_id": "uuid",
        "item_id": "uuid",
        "event_ids": ["uuid"]
      }
    }
  ],
  "total": 31,
  "truncated": true,
  "projection": {
    "name": "session_search",
    "version": 1,
    "strategy": "structural_lexical"
  },
  "coverage": {
    "retention": "trajectory",
    "searched_resources": 148,
    "content_complete": true
  },
  "warnings": []
}
```

### Searchable fields

| Kind | Default indexed fields |
|---|---|
| `user_message` | Visible user text |
| `assistant_message` | Visible assistant text, excluding private reasoning |
| `tool_call` | Tool name, normalized arguments, command, query, and paths |
| `tool_result` | Visible result text and normalized failure fields |
| `file_change` | Normalized path, operation, patch/write input, observed result |

Large values use bounded field-aware extraction rather than one head-only
character slice. Truncation is recorded in coverage metadata. Document
construction honors `mode` and `kinds`: path mode materializes only file-path
documents, and kind-filtered searches do not serialize unrelated tool inputs
or results. `searched_resources` therefore counts the documents actually
searched rather than every searchable document in the session.

### Ranking

Version 1 ranks deterministic structural signals before any future semantic
stage. Field-weighted lexical relevance is combined with boosts for user
requests, mutations, failures, validation commands, and recency. There is no
embedding or model dependency. Ranking does not use item outcome as a proxy for
truth or hide failed evidence. Results are sorted by descending score with
timestamp and canonical ID tie-breakers.

Search-generated tool calls and outputs remain observable canonical events but
recognized `ct session summary/search` commands are excluded from search
documents. An explicit future kind may expose them for audit.

### Expansion

Search never embeds unbounded content:

1. Search returns a bounded snippet and canonical references.
2. `session.items` expands a reconstructed item.
3. `session.events` resolves exact source events or detached tool results.

No second message-number identity such as `#12` is introduced. UI clients may
show ephemeral row numbers, but UUIDs remain the only references.

## Future Scope Methods

The search engine may later support:

- `graph.search`: the selected orchestration run, including spawned agents but
  excluding ordinary conversation forks;
- `project.search`: multiple sessions in one normalized project;
- `project.search` filters for vendor, time range, status, and model.

These are separate methods because widening scope changes discovery cost,
coverage, pagination, and result interpretation. `session.search` does not gain
a `scope=project` escape hatch.

## Compatibility and Cleanup

### Existing contracts

- Existing v2 methods and plugin requirements remain unchanged.
- `session.overview` is not renamed and does not become an alias of summary.
- `session.items` and `session.events` retain their current request behavior.
- `living.events` retains its frozen `view|details` protocol.
- Adding v1 methods does not imply that unrelated v2 methods become v3.

### Follow-up cleanup candidates

After the new reading flow is validated, a separately approved method-version
migration may:

- replace ambiguous session/root/turn entry-point aliases with explicit scope;
- add consistent `coverage`, `warnings`, and truncation metadata where responses
  are already bounded;
- align graph response models with graph semantics instead of reusing session
  response types;
- retire fields only after plugin manifests and external consumers can declare
  the replacement method versions.

These changes must not be bundled into the first summary/search implementation.

## Delivery Plan

### Phase 0: Contract inventory and terminology

- Document the method taxonomy and reading flow.
- Treat the registry and Pydantic schemas as the contract authority.
- Record existing consumers through plugin manifests and CLI registration.
- Correct stale method counts and list all registered method families in the
  architecture and CLI documentation.

### Phase 1: Session summary — implemented

- Add Pydantic request/response models and `session.summary.v1` registration.
- Build the projection from canonical session, item, event, and activity facts.
- Add `ct session summary SESSION_ID` with markdown and compact JSON outputs.
- Benchmarking exact objective, change, command-outcome, and verification fact
  preservation remains follow-up work.

### Phase 2: Session search — implemented in memory

- Define one internal searchable-document representation in the analysis layer.
- Implement bounded in-memory lexical search over one targeted session.
- Add `session.search.v1` and `ct session search SESSION_ID QUERY`.
- Add synthetic reference-resolution, ranking-quality, response-size, and
  warm-store timing evaluation. Audited provider-log evaluation remains
  follow-up work.

### Synthetic behavioral benchmark

The first evaluation phase uses deterministic canonical `SessionGraph` fixtures
and does not read provider logs. Run it with:

```bash
uv run python scripts/benchmark-session-retrieval.py
```

The benchmark validates summary fact preservation, corrected and unresolved
failure state, latest-plan semantics, evidence resolution, scope isolation,
private-reasoning and self-retrieval exclusion, long-field head/tail search,
truthful truncation, and deterministic output. Search relevance reports
Recall@10, MRR, and nDCG@10 for the current structural-plus-lexical strategy,
a lexical snippet baseline, and a recent-first baseline. Warm-store latency and
response sizes are diagnostic only in this synthetic phase.

Results are explicitly labeled behavioral rather than real-world retrieval
quality. Audited provider-log evaluation remains deferred until representative
sources are available.

### Private local evaluation workflow

The synthetic benchmark remains the committed deterministic baseline. A second
benchmark evaluates selected local canonical sessions without exporting their
content:

```bash
cp benchmarks/session-retrieval-local.example.json \
  .artifacts/session-retrieval-local/judgments.json
# Replace placeholders with source-audited local session IDs, canonical
# item/event references, and private query text.
uv run python scripts/benchmark-session-retrieval-local.py \
  --config .artifacts/session-retrieval-local/judgments.json
```

The configuration explicitly lists sessions and contains source-derived
summary assertions and graded search relevance. It is intentionally local:
never commit local IDs, source mappings, prompts, paths, snippets, raw logs,
or detailed reports. The benchmark uses normal discovery, canonical ingestion,
and service dispatch, but writes its report only below the ignored
`.artifacts/session-retrieval-local/` directory. The committed example uses
synthetic UUIDs and placeholders only.

For each configured query it reports source-order candidate Recall@5 and
Recall@10 as prefix diagnostics, plus candidate-universe recall as the measure
of lexical matching completeness, separately for exact and paraphrase tiers.
The source-order prefix is not API-ranked recall. Ranking reports actual
Recall@5, Recall@10, MRR, nDCG@10, and returned-result precision for
structural-plus-lexical, lexical-only, and recent-first orders over the same
returned candidate set. This keeps candidate coverage distinct from ordering
quality. The current lexical candidate stage has no semantic retrieval claim;
poor paraphrase recall is a diagnostic gap, not a reason to add embeddings in
Phase 1/2.

Each local run also checks canonical reference resolution, selected-turn scope,
private-reasoning and self-retrieval exclusion, kind filtering, bounds,
coverage metadata, and repeat determinism. It records at least 30 warm calls
for summary plus representative search cases. These timings are diagnostic
until a stable representative baseline exists.

### Phase 3: Persistent index and wider scopes

- Add a rebuildable SQLite FTS projection when measured session sizes justify it.
- Reuse the same match contract for `graph.search` and `project.search`.
- Integrate revisioning and lazy detail hydration with the retained read model.

### Phase 4: Agent integrations

- Expose the versioned methods through a thin MCP or vendor integration.
- Keep vendor lifecycle hooks outside canonical core.
- Evaluate summary plus search against raw-log and existing CLI baselines before
  considering any runtime compaction integration.

### Phase 5: Versioned legacy cleanup

- Use observed consumers and benchmark evidence to propose narrow v3 method
  migrations.
- Keep compatibility adapters until all first-party plugin requirements move.

## Acceptance Criteria

- `summary`, `overview`, `search`, and detail have non-overlapping documented
  responsibilities.
- Every summary claim and search result resolves to canonical evidence.
- A session method never silently includes child agents or sibling forks.
- Summary and search outputs are deterministic for unchanged canonical input.
- Bounded output reports truncation and incomplete content coverage truthfully.
- Existing v2 clients continue to work when the new methods are introduced.
- Search and summary remain derived projections and add no presentation fields
  to canonical models.
- Performance and quality are compared against committed source evidence rather
  than updating expected values from current output alone.
