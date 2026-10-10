# Frozen Core protocol

## Baseline

[`validation/core-protocol.json`](../validation/core-protocol.json) freezes the
public Core boundary. It records all 18 methods, method versions, request/result
JSON Schemas, and the local API envelopes. The retired published-facts schema
is not a public dependency of local canonical queries.
CLI projections and plugin protocols are separate consumer contracts.

The local-only revision increments every method version by one. Local success
envelopes contain `result`, not the parked remote envelope's `data`. Transport
metadata is `local` / `live` / `retained`, with no saved-view or snapshot identity.
Remote delivery is unavailable for all 18 methods through one capability declaration.

Collection pagination uses opaque unsigned count-keyset cursors bound to query
and method version. Inventory order is identity-based; content uses canonical
source order and ID tie-breakers. Determinism applies per call, not across live
changes. Published-view references and stale-view errors are retired. Method
request/result schemas remain frozen and reviewed; removing a storage protocol
does not authorize arbitrary changes to those schemas.

Run the gate from the repository root:

```sh
uv run python scripts/check-core-protocol.py
```

Core CI runs the same command. The gate rejects unreviewed method additions,
removals, version changes, and schema changes. Additive changes require review too.
Shared `$defs` and compact schema serialization reduce file size, not constraints.

## Change a contract intentionally

1. Propose the affected boundary and consumers.
2. Provide implementation or provider evidence for the change.
3. Explain why the change belongs in Core, not a plugin's interpretation layer.
4. Specify versioning, compatibility, migration, and qualification requirements.
5. Obtain approval before changing the frozen boundary.
6. Implement the approved change and required version bump.
7. Update the snapshot:

   ```sh
   uv run python scripts/check-core-protocol.py --update
   ```

8. Review the snapshot diff as a public protocol change.
9. Run the gate again:

   ```sh
   uv run python scripts/check-core-protocol.py
   ```

Never update the snapshot only to make CI pass.

## Stateless living revision

The approved living redesign changes only two method contracts:

| Method | Method version | Result schema |
| --- | --- | --- |
| `living.sessions` | 5 | `ct.living_sessions.v3` |
| `living.events` | 3 | `ct.living_events.v2` |

Both use `cursor` and count-bounded live keyset pages, with `total`, `returned`
and `next_cursor`. The old `after`, `through`, `changes`, revisions and reset
operations are removed rather than silently reinterpreted. Old method versions
are rejected; cursors remain query- and version-bound.

`living.sessions` returns `items` from topology and file metadata without
transcript ingestion. Its optional run/session/project scopes are mutually
exclusive. A rolling `horizon_days` of 1–30 (default 3 / 72 hours) applies to
global/project scopes; explicit runs ignore it. Each item's digest covers all
other public fields, including its own source's time-derived `living`/`inactive`
state.

`living.events` requires exactly one `root_session_id` or `session_id` and allows
`turn_id` and/or `item_id` to narrow within that run. A missing or cross-run
narrowing ID returns `resource_not_found`; Core never searches other runs.
The response contains `resources`, each with kind, path, payload and digest.
The digest covers the normalized retained details payload in both modes, not
transport or cursor fields. Completing an existing tool item is therefore
observable without a stored change feed. Canonical source ordering is preserved
per call.

Consumers compare complete passes and persist their own digests when needed.
No cross-page snapshot is promised. Loop migrates old watch continuation state
by rebaselining once, without evaluating historical resources during that pass.
The other 16 methods, their metrics, and the API envelope are unchanged.

## Plugin boundary

Plugins can challenge incomplete contracts with concrete evidence and the same
approval procedure. They must not silently change Core, duplicate construction
or metric formulas, add compatibility shims, or weaken ownership boundaries.
Until approval and versioning are complete, adapt within the plugin's product
layer or report the incompatibility. See [architecture](architecture.md).
