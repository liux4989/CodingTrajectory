# Session graph navigation restructure

The session workspace presents four peer tabs (Timeline, Context window,
Conversation tree, Agent graph) over scopes that the [PRD](prd.md) defines as a
hierarchy: `SessionGraph -> Session -> Turn -> Item`. Two problems follow:

- **Feature overlap.** Timeline inlines child-agent activity and offers a
  cross-agent filter; the Agent graph session-composition tooltip duplicates
  per-session context/cost facts; Conversation tree and Agent graph both render
  session hierarchies. The PRD requires session-scoped projections first,
  graph-scoped second, and forbids presenting child-agent turns or context as
  if they belonged to the root session.
- **Flattened navigation.** All four tabs hang off
  `/sessions/$sessionId?view=...`, so the URL never says which scope is shown,
  and cross-tab jumps (composition bar -> `view=context`, tree row ->
  `view=graph`) silently swap the session under the same tab id.

## Target structure

Two route levels mirroring the canonical hierarchy. Graph identity is the root
session id.

| Route | Scope | Content |
| --- | --- | --- |
| `/graphs/$rootId` | Graph | Fork tree + agent orchestration + aggregate usage with explicit per-session sections |
| `/graphs/$rootId/sessions/$sessionId` | Session | `?tab=context` (default) or `?tab=timeline`; timeline keeps `kind/artifact/vendor/outcome/entry` search params |

`/sessions/$sessionId` remains as a resolver: it loads the graph payload and
redirects to the canonical `/graphs/$rootId/...` address, so existing links and
legacy `/sessions/$id/graph|timeline|tree|context-window` URLs keep working.

## Graph page sections

Summary-to-detail order:

1. Orchestration stat cards (kind, sessions, spawned agents, tokens/cost).
2. Conversation branches (the former Conversation tree). Selecting a branch
   sets `?branch=<id>` and scopes sections 3-4 to that branch; each branch owns
   its agent graph.
3. Agent hierarchy (`GraphTree`), filtered to the selected branch.
4. Session composition: stacked cached/uncached processed-token bars per
   session — the PRD-required explicit per-session section. The tooltip keeps
   role, processed, cached share, and estimated cost; context-used and turns
   move to the session scope. Clicking a bar navigates down to the session
   route.
5. Graph usage totals: bucket-mix donut and per-model bars.

## Session page

Breadcrumb `Graph <rootId> / Session <shortId>` replaces the four-tab
switcher; two sub-tabs:

- **Context window** (default): unchanged summary, maintenance details, and
  context event explorer. This is the only home for per-session context
  pressure.
- **Timeline**: evidence timeline scoped to the session. Child-agent entries
  render as spawn references linking to the child session; the cross-agent
  filter and cross-session waterfall jump are removed. Turn/item drill-down via
  the `entry` search param completes the `Session -> Turn -> Item` descent.

## Execution phases

1. Add `/graphs/$rootId` with merged conversation branches and trimmed
   composition tooltip; old views keep working.
2. Add `/graphs/$rootId/sessions/$sessionId` with Context/Timeline tabs;
   rewire bar-click and branch-row navigation.
3. Timeline scoping cleanup (spawn-reference links; drop cross-agent filter and
   cross-session waterfall jump).
4. Convert `/sessions/$sessionId` to a resolver, retarget legacy redirects,
   update all link sites (sessions list, overview, model usage, session link,
   command palette), delete the four-tab switcher.
5. Verify web typecheck/build; run the metrics quality gate if metric-sensitive
   paths change.

No backend or API changes: the graph, tree, context-window, and evidence
timeline payloads are reused as-is.
