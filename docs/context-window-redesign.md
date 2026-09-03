# Context Window UI Redesign

**Status:** Implemented

**Reviewed:** 2026-09-03

**Primary surface:** `packages/plugins/datahub/web/src/routes/context-window.tsx`

## Purpose

The context-window page should be a diagnosis surface, not a second raw session timeline. The session timeline already owns chronological inspection. This page should help a user answer four questions, in this order:

1. How full is the context window?
2. What consumes the most context?
3. Where is context pressure or churn occurring?
4. What evidence explains those numbers?

The redesign keeps the existing backend projection authoritative. The frontend presents available facts and degraded states; it must not infer new semantic categories from command text.

## Current Findings

The current implementation is functional, but its visual hierarchy gives nearly every element equal weight:

- The capacity composition chart dominates the first viewport, even when only one observation exists.
- Context history and the useful event evidence begin below the fold.
- Every event group starts expanded, creating a long, noisy list.
- The page has nested scrolling in the event list and inspector.
- The first event is selected automatically, so the inspector initially shows a broad starting-context item instead of a useful summary.
- Hover, selection, and pinning create three competing interaction states.
- The route receives useful projection fields such as `model`, `token_cost`, `expensive_items`, `provider_usage_buckets`, and `warnings`, but does not present them.

The fixed inspector and dense charts make the page feel like an analytics dashboard before it has established the basic answer: whether the session is near its context limit and why.

### Degraded capacity state

When total capacity is unavailable, the current page can render combinations such as observed tokens against `0`, an empty capacity bar, and “unknown used.” This visually implies a measured denominator that does not exist.

The degraded state must instead say that observed tokens are available while model capacity is unavailable. Category composition remains useful, but it is normalized against observed tokens rather than drawn as a fraction of an unknown capacity.

### Label quality boundary

Some event labels contain only a generic target, while others include malformed serialized fragments. The route should display the projected behavior and supporting identity supplied by the backend. It should not add a growing set of TypeScript regular expressions or command taxonomies to repair labels locally.

Malformed or incomplete identities must be corrected at the shared projection boundary so overview, summary, metrics, and this UI remain aligned.

## Target Experience

```text
Context window                                      Model and evidence status
23.5K used of 258.4K · 9.1% · 234.9K remaining
[ composition bar, including unused capacity when known ]

Largest contributors                    Context pressure
Files             8.4K                  2 cache breaks
Tool output       6.1K                  1 compaction
Starting context  4.7K                  +18.2K since first prompt

Context history                         [Search history] [Filters]
> Before first prompt · 7 events · 5.2K
v Turn 1 · 18 events · 14.1K
    Read file                                      +1.2K
    discovery.py · Files · terminal-visible
```

The target hierarchy is:

1. **Capacity** — a compact, truthful status header.
2. **Drivers** — the largest contributors and expensive items.
3. **Pressure** — cache breaks, compactions, warnings, and growth.
4. **Evidence** — a searchable, grouped history with on-demand detail.

## Presentation Behavior

### Capacity known

Show:

- used tokens, capacity, percentage, and remaining tokens;
- category segments as a fraction of capacity;
- unused capacity as an explicit neutral segment;
- an approximation marker only when the backend says the value is estimated.

Do not repeat the same figures in multiple oversized cards.

### Capacity unavailable

Show:

- observed tokens;
- “capacity unavailable” with the model name when known;
- category composition normalized against observed tokens;
- the evidence or warning explaining the unavailable capacity, when supplied.

Do not render unused capacity, a zero denominator, a percentage, or the word “unknown” as if it were a measurement.

### Multi-session scope

When the projection includes child sessions, show a compact scope strip identifying root and child contributions. Never merge a child context window into the root as though they share one physical model context.

## Summary and Contributor Area

The first content row below capacity contains two compact cards.

### Largest contributors

Use existing projection fields directly:

- category totals;
- `expensive_items`;
- `token_cost`;
- `provider_usage_buckets`, where present.

With no event selected, this area is the page-level explanation. Selecting an event replaces it with event detail. Remove the separate pin state; click selects, clicking again or using Escape returns to the summary.

### Context pressure

Show a compact summary of:

- cache-break count and affected tokens;
- compaction count and known before/after sizes;
- warnings;
- context growth between meaningful observations.

Detailed cache-break and compaction records belong in an accordion below the summary. Only render trend charts when multiple comparable observations exist. A single observation should be a row, not a chart. If compaction sizes are absent, say “size not exposed” instead of implying an outcome.

## Context History Explorer

### Group defaults

- “Before first prompt” starts collapsed and displays its event count and token total.
- The latest meaningful turn starts expanded.
- Other turns start collapsed.
- Group names use natural product language rather than raw enum values.

### Event rows

Each row separates behavior from identity:

```text
Read file                                          +1.2K
discovery.py · Files · terminal-visible
```

The first line answers what happened. The supporting line answers which target, category, and evidence visibility apply. Color is supplementary; text carries the meaning.

The route consumes the structural labels produced by the shared projection. It must not maintain a parallel frontend classifier for shell commands or tool families.

### Filtering

- Place search in the context-history header.
- Give search an explicit accessible label; placeholder text is only a hint.
- Use an installed multi-select `ToggleGroup` for category filters.
- Display result count and a clear reset action.
- Preserve group boundaries while filtering.

## Responsive and Scrolling Model

Use one page-level scroll container.

- Desktop: history and detail form a two-column layout; detail may be sticky within the page.
- Mobile: event detail opens in the installed `Sheet` component.
- Avoid independently scrolling the history list and detail panel.
- Initially limit very large groups with “Show more.” Add virtualization only if measured session sizes require it.
- `content-visibility: auto` may be used as a progressive rendering optimization after browser validation.

## Accessibility Corrections

- Do not place interactive category controls inside an element with `role="img"`.
- Give search and filters programmatic labels.
- Do not use `role="tab"` for navigation links unless the complete tablist, tab, and tabpanel keyboard model is implemented.
- Keep visible focus states for rows, filters, accordions, and mobile detail controls.
- Expose chart values as adjacent text, not color or hover content alone.
- Verify reading order and reflow at 200% zoom.

## Component Plan

Keep the route responsible for data loading, URL/query state, filters, and selection. Extract only three product-level sections:

1. `ContextWindowSummary` — capacity, composition, contributor summary, and degraded state.
2. `ContextEventExplorer` — search, filters, groups, rows, selection, and responsive detail.
3. `ContextMaintenanceDetails` — cache breaks, compactions, provider observations, and warnings.

Reuse the installed `Card`, `Accordion`, `ToggleGroup`, `Input`, `Badge`, and `Sheet` primitives. Add an `Alert` primitive only if warnings need a distinct semantic surface.

This split should materially reduce the current route size while avoiding a component per visual fragment.

## Implementation Sequence

### Phase 1 — Correctness and hierarchy

- Implement explicit known-capacity and unavailable-capacity states.
- Replace the oversized first-viewport chart layout with capacity, contributors, and pressure summaries.
- Present the existing model, cost, expensive-item, provider-bucket, and warning fields.
- Stop auto-selecting the first event and remove pinning.
- Keep label semantics sourced from the shared projection.

### Phase 2 — Interaction and structure

- Extract the three product-level sections.
- Add collapsed group defaults, search, category filters, and result counts.
- Move maintenance details into an accordion.
- Replace nested scrolling with page scrolling.
- Add the mobile detail sheet.

### Phase 3 — Validation

- Run `bun run check:api`.
- Run `bun run build`.
- Validate known-capacity and unavailable-capacity sessions.
- Validate sessions with and without cache breaks, compactions, child sessions, provider observations, and warnings.
- Exercise keyboard navigation, light and dark themes, narrow layouts, and 200% zoom.
- Confirm malformed labels are fixed upstream rather than hidden by route-specific cleanup.

## Acceptance Criteria

- A user can identify capacity status and the largest contributor without scrolling.
- Unavailable capacity never renders as zero capacity, a percentage, or unused space.
- Cache breaks and compactions are visible without dominating the page.
- The initial state explains the whole session rather than an arbitrary first event.
- Event rows state both behavior and target identity when that evidence exists.
- The route contains no command-text taxonomy or label-repair rules.
- The page has one scrolling model and remains usable on mobile.
- Search, filters, disclosure controls, and details are keyboard accessible.
- The context page complements rather than duplicates the session timeline.

## Non-goals

- Reclassifying commands in the browser.
- Reconstructing missing capacity, compaction outcomes, or token counts.
- Replacing the raw chronological session timeline.
- Adding speculative charts for a single observation.
- Preserving the current pin interaction solely for compatibility.
