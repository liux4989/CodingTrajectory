# Bulk Primitive Query Handoff

## Context

The `code-time` plugin needs to collect data for many sessions, then build a
plugin-owned report for the CLI and web UI. The current plugin-side workflow
uses public core commands as primitives:

- `ct project list`
- `ct project sessions`
- `ct session stats`
- `ct session usage`

The original optimization used shell batching with `xargs`, stdin, and many
subprocesses. That was brittle: pipe handling failed, concurrent stdout parsing
was fragile, and every subprocess repeated discovery and ingestion work.

Do not move code-time business logic into core. The desired boundary is:

- Plugin web: frontend.
- `packages/plugins/code_time/code_time.py`: plugin server/controller and report
  business logic.
- Core `ct`: stable primitive data provider.
- Core service layer: shared local service/dispatch implementation behind the
  CLI transport.

## Design Direction

Core should expose bulk primitive reads, not plugin workflows.

The CLI is already a thin adapter over a service-like core:

- argparse parses flags and merges `--params`.
- `contracts.py` validates request and response schemas with Pydantic.
- `service.py` resolves stores/cache and dispatches method handlers.
- CLI renderers convert service payloads to markdown or compact JSON.

That architecture is acceptable. The weak point is that repeated primitive
queries are only efficient if callers invent their own batching. Fix this in the
core service layer with reusable selectors and collection-shaped methods.

## Command Shape

Prefer resource-shaped commands and use `--params` for query mode.

Target command set:

- `ct project list`
- `ct project sessions`
- `ct session data`
- `ct session events`
- `ct session items`

Deprecate or remove operation-shaped commands where a resource-shaped command
can cover the same behavior:

- Replace `ct session event-scan` with `ct session events --params ...`.
- Consider replacing single `event-detail` with `ct session events` using
  `event_ids`.
- Consider replacing `item-detail` with `ct session items` using `item_ids`.

Keep existing human-friendly commands where useful, but route them through the
same service primitives or mark old names as compatibility aliases if we need a
migration window.

## Proposed `ct session data`

Purpose: bulk read reusable session facts for many sessions. This is the main
primitive needed by `code-time`.

Example by project scope:

```bash
ct session data --global-scope --output json --params '{
  "project_name": "coding-trajectory",
  "since_days": 1,
  "agent_vendor": "codex_cli",
  "include": ["metadata", "runtime", "usage"]
}'
```

Example by explicit ids:

```bash
ct session data --global-scope --output json --params '{
  "session_ids": ["019e...", "019f..."],
  "include": ["metadata", "runtime", "usage", "stats"]
}'
```

Request fields:

- `session_id`: optional single session entrypoint.
- `session_ids`: optional list of session entrypoints.
- `project_name`: optional project selector.
- `since_days`: optional recent-window selector.
- `modified_since`: optional timestamp selector.
- `agent_vendor`: optional vendor selector.
- `include`: list of requested sections.
- `extra_billing`: optional usage-cost toggle, matching existing usage command.

Initial `include` values:

- `metadata`: id, project, title, vendors, session ids.
- `runtime`: status, start/end, execution seconds, wait seconds, turns, items,
  tool calls, failed tools, subagent sessions, compactions, interruptions,
  rollbacks, average time to first token.
- `usage`: total token usage and cost from existing `session.usage` logic.
- `stats`: context/model/message/quota facts from existing `session.stats`
  logic. Avoid requiring this for code-time unless it truly needs context
  composition.

Response shape:

```json
{
  "items": [
    {
      "id": "019e...",
      "project": "coding-trajectory",
      "title": "Add waiting interval metrics",
      "vendors": ["codex_cli"],
      "sessions": ["019e..."],
      "runtime": {},
      "usage": {},
      "stats": {},
      "warnings": []
    }
  ],
  "errors": [
    {
      "id": "019e...",
      "message": "resource not found"
    }
  ]
}
```

Bulk commands should return partial results. A bad session should not abort the
entire response unless request validation itself fails.

## Proposed `ct session events`

Purpose: one event collection primitive that handles both scan and detail.

Examples:

```bash
ct session events --output json --params '{
  "session_id": "019e...",
  "type": "tool.call.succeeded",
  "filters": ["tool=exec_command"]
}'
```

```bash
ct session events --output json --params '{
  "event_ids": ["...", "..."]
}'
```

Request fields:

- `session_id`, `root_session_id`, or `turn_id`: optional scope.
- `event_ids`: optional explicit ids.
- `type`: optional event type filter.
- `filters`: optional list of `KEY=VALUE` filters.
- `limit`: optional result limit.

This replaces `event-scan` and can also cover `event-detail` once explicit id
detail mode returns full event payloads.

## Proposed `ct session items`

Purpose: one item collection primitive.

Examples:

```bash
ct session items --output json --params '{"item_ids":["...","..."]}'
```

```bash
ct session items --output json --params '{
  "session_id": "019e...",
  "types": ["tool_call"]
}'
```

Start with explicit `item_ids` parity with current `item-detail`; add scoped
query filters later only if a plugin needs them.

## Core Implementation Notes

Add shared selector/resolver code instead of per-command batch mechanisms.

Suggested service helpers:

- `resolve_session_graphs_for_query(params, context) -> list[SessionGraph]`
- `resolve_session_graphs_for_ids(session_ids, context) -> list[SessionGraph]`
- `session_graph_metadata(graph) -> dict`
- `session_graph_runtime(graph) -> dict`
- `session_graph_usage(graph, extra_billing=False) -> dict`
- `session_graph_stats(graph) -> dict`

The bulk resolver should:

- Use targeted cached paths for explicit `session_ids` when possible.
- Use one full discovery pass for project/time/vendor selectors.
- Deduplicate root session graphs.
- Update `IndexCache` for all returned sessions/turns.
- Preserve public id rendering with `_public_output_for_session_graph`.

Do not make each CLI command implement its own dispatch. Add service handlers
and Pydantic contracts, then let CLI registration stay thin.

## Code-Time Migration

After `ct session data` exists, update
`packages/plugins/code_time/code_time.py`:

1. Keep `build_report()` as the plugin-owned business workflow.
2. Replace repeated `session stats` / `session usage` subprocess calls with one
   `ct session data` call.
3. Request only `["metadata", "runtime", "usage"]` unless the UI needs context
   composition.
4. Keep report aggregation, project grouping, totals, caching, and rendering in
   the plugin.
5. Keep `code_time_web.py` calling `build_report()`; do not make the web call
   core directly.

## Migration Plan

1. Add contracts for `session.data`, `session.events`, and optionally
   `session.items`.
2. Implement `session.data` first because it removes the code-time batching
   pressure.
3. Register `ct session data` and expose `--schema`.
4. Update code-time to consume `session.data`.
5. Add `ct session events` as the replacement for `event-scan`.
6. Decide whether old names are removed immediately or kept as aliases.
7. Update `docs/cli.md` and `docs/plugin.md`.

## Verification Checklist

Run:

```bash
uv run ct session data --schema
uv run ct session data --global-scope --output json --params '{"project_name":"coding-trajectory","since_days":1,"include":["metadata","runtime","usage"]}'
uv run ct plugin code-time --window today --project coding-trajectory
uv run ct plugin code-time --window today --output json
uv run ct session events --schema
uv run ct session events --output json --params '{"session_id":"<id>","type":"tool.call.succeeded"}'
```

Also run Python compile checks on changed files:

```bash
uv run python -m py_compile packages/core/src/coding_trajectory/contracts.py packages/core/src/coding_trajectory/service.py packages/cli/src/coding_trajectory_cli/commands/session.py packages/plugins/code_time/code_time.py
```

Do not add unit tests for this repo task.

## Review Findings After Initial Implementation

These findings were observed after commit
`3ca7810 Implement bulk primitive session queries and migrate code-time plugin`.
They should be handled before treating the bulk primitive work as complete.

1. `session.data` does not use targeted cache loading for explicit
   `session_ids`.

   `code_time.py` now calls `ct session data` with only `session_ids` and
   `include`. In `service.py`, `resolve_store()` still targets the cache only
   through `_session_graph_entrypoint_id(params)`, which does not inspect
   `session_ids`. The result can be a broad global discovery for each bulk
   chunk, preserving correctness but undermining the main performance goal.

   Resolved in two passes. First, `resolve_store()` gained a bulk branch that
   calls `_resolve_bulk_cached_paths(bulk_ids, cache)` and routes the result
   through `_build_store_targeted`. Second, the resolver was tightened so
   that (a) a malformed id does not abort resolution, (b) a valid-but-uncached
   id contributes no paths instead of forcing a fall-back to full discovery,
   (c) `None` is returned only when the cache path index itself is empty, and
   (d) `_build_store_targeted([])` now short-circuits to an empty
   `DocumentStore` so discovery no longer raises "no valid log files found
   for paths: []" on an empty targeted set. With the fix, a bulk request
   whose ids are all malformed or uncached returns a well-formed partial
   response with `items: []` and per-id `errors` entries instead of a CLI
   error.

2. Malformed explicit ids can abort `session.data` instead of returning partial
   errors.

   Reproduction:

   ```bash
   uv run ct session data --global-scope --output json --params '{"session_ids":["does-not-exist"],"include":["metadata","runtime","usage"]}'
   ```

   Current behavior is a CLI error like `badly formed hexadecimal UUID string`.
   Expected bulk behavior is a successful response with `items: []` and an
   `errors` entry for the bad id.

   Suggested fix: normalize explicit ids inside a guarded helper, and collect
   malformed ids into the bulk `errors` array before matching.

3. `ct session items` does not accept `--global-scope`.

   Reproduction:

   ```bash
   uv run ct session items --global-scope --output json --params '{"session_id":"<id>","types":["tool_call"]}'
   ```

   Current behavior is an argparse error because the new `items` command uses
   `add_base_output_flags()`. This is inconsistent with `session data` and
   `session events`, and limits the command as a replacement for scoped item
   queries.

   Suggested fix: use `add_output_flags(session_items)` unless there is a clear
   reason item queries must stay local-only.

4. `packages/plugins/code_time/web/package-lock.json` has uncommitted drift that
   is inconsistent with `package.json`.

   The lockfile removes entries for dependencies that are still declared in
   `package.json`, including `2` and `recharts`. Regenerate it with the matching
   package manager command or revert the lockfile before committing.

5. Local plugin verification may require registering built-ins first.

   In one review run, `uv run ct plugin code-time ...` failed with
   `Plugin not found: code-time` because the local plugin registry was empty.
   Running `uv run ct plugin register-builtins` restored the expected route.
   This is local state, not necessarily a code bug, but the verification steps
   should mention it when running on a fresh profile.
