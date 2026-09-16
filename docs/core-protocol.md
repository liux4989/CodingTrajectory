# Frozen Core protocol

## Baseline

The public Core protocol is frozen by
[`validation/core-protocol.json`](../validation/core-protocol.json). The
snapshot records the complete 18-method registry, each method version and exact
request/result JSON Schema, the `ct.core.v1` success and error envelopes, and
the complete `ct.published_facts.v1` schema. CLI convenience projections and
consumer-owned plugin protocols are not Core protocol authority.

Run the review gate with:

```bash
uv run python scripts/check-core-protocol.py
```

Core CI runs the same command. It fails on any method addition or removal,
method-version change, request/result shape change, envelope change, or
published-fact shape change. This makes additive drift visible too; compatibility
rules do not make an unreviewed addition acceptable.

## Intentional changes

An intentional change starts with an explicit design proposal that identifies:

1. the affected Core/published-fact boundary and consumers;
2. concrete implementation or provider evidence for the change;
3. the compatibility and method, envelope, or fact versioning consequence;
4. migration and qualification work, with ownership outside Core called out;
5. why the change belongs in canonical construction or native metrics rather
   than a plugin enrichment layer.

After approval, implement the change, apply the required version bump, and run:

```bash
uv run python scripts/check-core-protocol.py --update
uv run python scripts/check-core-protocol.py
```

Review the baseline diff as a protocol artifact. Never update it only to make
CI pass.

## Plugin feedback rule

Plugin implementations are encouraged to challenge a frozen Core contract when
concrete implementation evidence shows that the boundary is incomplete or
wrong. The plugin owner must raise the explicit proposal above before changing
Core. A plugin must never silently edit Core protocols, add compatibility shims,
duplicate Core construction or metric semantics, or weaken the Core/Loop
boundary. Until a proposal is approved and versioned, the frozen Core contract
remains authoritative and the plugin adapts only within its own product-owned
layer or reports the incompatibility.
