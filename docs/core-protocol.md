# Frozen Core protocol

## Baseline

[`validation/core-protocol.json`](../validation/core-protocol.json) freezes the
public Core boundary. It records all 18 methods, method versions, request/result
JSON Schemas, and the local API envelopes. The retired published-facts schema
is not a public dependency of local canonical queries.
CLI projections and plugin protocols are separate consumer contracts.

The approved complete-local revision uses `ct.api.v2` and increments every
method version by one. Success envelopes contain `result`; constant transport
metadata is removed. Old explicit API and method versions are rejected.

Collections return all matching resources by default, in deterministic order.
An optional positive `limit` selects results without a hard maximum. `cursor` and
`next_cursor` are removed; overview also removes `page` and per-turn collection
caps. Optional selection retains honest total/returned counts where applicable.
Overview exposes text shortening separately from turn selection. Canonical detail
and search have complete available normalized content; previews stay bounded.

The public schema changes are deliberate. Request/result schemas remain frozen
and reviewed; future changes still require the procedure below.

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

## Complete local living revision

Living result identifiers advance alongside the common method/envelope revision:

| Method | Method version | Result schema |
| --- | --- | --- |
| `living.sessions` | 6 | `ct.living_sessions.v4` |
| `living.events` | 4 | `ct.living_events.v3` |

Both return complete scoped collections with `total` and `returned`; an
explicit `limit` can select results. There are no continuation cursors. The
`living.events` view/details mode and content-reference indirection are removed:
requests and assistant responses are direct strings, and item shapes retain full
structured values inline. Removed fields and old method versions are rejected.

`living.sessions` returns `items` from topology and file metadata without
transcript ingestion. Its optional run/session/project scopes are mutually
exclusive. A rolling `horizon_days` of positive days (default 3 / 72 hours) applies to
global/project scopes; explicit runs ignore it. Each item's digest covers all
other public fields, including its own source's time-derived `living`/`inactive`
state.

`living.events` requires exactly one `root_session_id` or `session_id` and allows
`turn_id` and/or `item_id` to narrow within that run. A missing or cross-run
narrowing ID returns `resource_not_found`; Core never searches other runs.
The response contains `resources`, each with kind, path, payload and digest.
The digest covers the full normalized public resource payload. Completing an existing tool item is therefore
observable without a stored change feed. Canonical source ordering is preserved
per call.

Consumers compare complete passes and persist their own digests when needed.
Each pass reflects its current source read; no cross-call snapshot is promised. Loop migrates old watch continuation state
by rebaselining once, without evaluating historical resources during that pass.
Native metric formulas are unchanged. Collection, detail, preview, and envelope
contracts migrate together.

## Plugin boundary

Plugins can challenge incomplete contracts with concrete evidence and the same
approval procedure. They must not silently change Core, duplicate construction
or metric formulas, add compatibility shims, or weaken ownership boundaries.
Until approval and versioning are complete, adapt within the plugin's product
layer or report the incompatibility. See [architecture](architecture.md).
