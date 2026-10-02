# Frozen Core protocol

## Baseline

[`validation/core-protocol.json`](../validation/core-protocol.json) freezes the
public Core boundary. It records all 18 methods, method versions, request/result
JSON Schemas, `ct.core.v1` envelopes, and the `ct.published_facts.v2` schema.
CLI projections and plugin protocols are separate consumer contracts.

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

## Plugin boundary

Plugins can challenge incomplete contracts with concrete evidence and the same
approval procedure. They must not silently change Core, duplicate construction
or metric formulas, add compatibility shims, or weaken ownership boundaries.
Until approval and versioning are complete, adapt within the plugin's product
layer or report the incompatibility. See [architecture](architecture.md).
