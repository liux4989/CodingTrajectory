# Tool identities, script executions, and content attribution

Tool identity, execution structure, source analysis, and content attribution are
independent. Familiar and unfamiliar tools follow the same rules. There are no
semantic alias categories, tool-specific display profiles, or fixed lists of
nested tools that source inspection accepts.

## Evidence from JSONL

| Provider | Invocation | Result / observation |
| --- | --- | --- |
| Claude Code | `tool_use.id`, `name`, `input` | `tool_result.tool_use_id`, content and optional native result metadata |
| Codex | `function_call` arguments or `custom_tool_call` input, joined by `call_id` | Matching output; native terminal events; optional originating-cell inventory |
| Pi | Assistant `toolCall.id`, `name`, arguments | `toolResult.toolCallId`, content, status and optional `nestedCalls` inventory |
| Amp | Message `tool_use` or live call hooks | Matching message result or reconciled result hooks |

Names identify tools; IDs join evidence. Provider decoders normalize recorded
native item types, arguments, outcomes, timings, and topology. They do not
reconstruct an executed action sequence from source or create synthetic child
items when a wrapper completes. Provider-native type decoding is distinct from
semantic display categorization.

## Shared execution layer

The canonical hierarchy is `Turn → execution unit → item references`.
`Turn.items` remains the flat evidence and measurement ledger. Execution units
contain direct references and script executions, without copied item bodies.

A script execution contains:

- Full source provenance: source field, content digest, original-log record
  indices/digests, provider identity, and an **Open source** link to the source
  record's line. Source is read losslessly from the log. Previews are separately
  bounded and sanitized; the complete source remains in the original JSONL.
- Observed external calls: recorded identities, argument previews and references,
  outcomes, timing, and result references. `ownership_basis` distinguishes a
  provider parent ID or host inventory from inferred ownership.
- Source-only call sites: source byte ranges, tool member names, static literal
  arguments where recoverable, and expression previews. These have no runtime
  outcomes or timings and do not contribute observed execution counts.
- Context references: owning request/turn, adjacent items, and preceding/following
  assistant items. Their bodies remain on the surrounding items.
- Invocation/runtime result: emitted-output references, lifecycle coverage,
  controls, and failure information. Invocation completion alone cannot establish
  completion of a yielded script runtime.

Standalone tools remain direct items. A shell command gets a lossless source
reference without becoming a second orchestration script merely because it has
`cmd` or `command` arguments. A nested shell operation remains an external call
of its owning script when the relationship is supported.

Analysis reads original logs and creates no source, argument, or result exports.
References identify records and their digests rather than copied evidence.
Previously exported files in the project or user cache are not required.

## Generic source and relationship analysis

JavaScript source is parsed with a syntax tree. Calls through `tools.*` and
literal `tools["name"]` members are recognized anywhere in valid syntax,
including branches, callbacks, loops, promises, and mixed-tool scripts. Unknown
names work without adding a registry entry. Strings and comments are not scanned
as executable code. Source is never evaluated.

Literal JSON-shaped arguments can be decoded. Variables, dynamic member names,
aliases, and other expressions remain unresolved and accessible in full source.
`source_analysis_coverage` is therefore partial even for a valid syntax tree;
syntax errors and unsupported languages are explicit. A call site is not an
execution, nor a claim about ordering or number of loop iterations.

Explicit provider relationships and host inventories establish observed
composition. For older native events lacking a parent ID, matching literal
argument evidence within a unique recorded script invocation interval can infer
ownership. Contradictory arguments or ambiguous parents remain unresolved.
`inferred_arguments_and_interval` states that inference separately from the
native call's observed occurrence. Multiple observed calls remain separate even
when they match one source call site.

`inventory_coverage` is complete only when the host claims completeness. Native
observations without that claim are partial; no observations or claim means
unknown. Empty inventories are never proof that a script invoked no tools.

## Generic tool details

Every tool retains its recorded name. Missing names use a structural item-kind
label. Detail extraction is independent of identity: shell arguments preserve
command text, quoting, pipelines, line boundaries and explicit working directory;
other inputs are inspected as bounded key/value paths, including nested arrays.
Text subjects precede envelope enums and numeric transport controls. Preview
redaction does not modify source evidence.

No tool is hidden because its name is unfamiliar. Direct calls retain individual
references. Script-owned children and runtime controls are represented by their
parent relationship, not hidden through a tool-name filter. An unowned control
call remains a direct item.

## Stats and usage

Execution composition and content attribution answer different questions:

| Projection | Meaning |
| --- | --- |
| Observed external tool composition | Recorded direct and nested calls grouped by exact tool identity |
| Source-only sites | Syntax evidence excluded from observed execution counts |
| Tool-call content categories | Independently measured canonical arguments/results grouped by recorded identity |
| Script source | Original script invocation input content |
| Unassigned emitted output | Script output whose bytes cannot be attributed to a child from recorded evidence |

Script UUIDs are navigation identities, not stats categories. Script membership
alone does not partition content or usage. Native telemetry represented by a
script envelope is marked with explicit content custody so its evidence does
not add a duplicate visible-content contribution. No wrapper measurement is
proportionally distributed among source-inferred tools. Allocated usage remains
with the script content parent; source/output children carry visible-content
measurements, without an invented usage split. Provider totals and billing
remain separately reported.

The stats context tree concerns the resident context after compaction. Its
compact execution counts concern the scoped session history. These scopes
must not be confused. Incomplete inventories are reported alongside observations.

## Existing command projections

Overview, summary, search, items, events, stats, usage, request/model/tool usage,
and corresponding graph views consume the same execution layer. No new command
is introduced. Selected child evidence includes the parent reference even when
pagination excludes its root. Item/event row counts retain their own meaning.
Text and JSON preserve the same relationships and navigation targets. Terminal
formatting never abbreviates a UUID inside a link or clips a link target.
Stats return compact script, observed-call, and source-only counts with inventory
coverage, without embedding execution units. Graph stats preserve per-session
counts and their aggregate. Flat runtime tool-item counts are labeled separately;
they are not observed execution counts. Use item detail for full script evidence
and tool usage for per-tool measurements. Stats do not infer success rates from
the absence of failure. Overview discloses when turn selection omits earlier turns.

## Qualification

Run `scripts/check-metrics-quality-gate.sh`,
`uv run python scripts/validate-metrics-baselines.py`, the protocol check, source
selection qualification, and the retrieval integration benchmark. Expected
metrics remain audited against committed provider evidence; current output alone
cannot justify changing expected values. Repository guidance prohibits unit tests.

Actual-session verification uses overview, stats, and item detail for
`01a12eb5-0efa-7d80-88ec-64bdce718cc3` and
`01a12ec1-bb50-7da2-b3e7-e05d2d173aab`, checking source-record digests, lossless
source extraction, observed/source-only separation, nested command arguments,
execution-layer retention in item-detail JSON, compact counts and coverage in
stats JSON, and the absence of script-ID categories.
These are live logs; snapshots and counts change as new turns append.
