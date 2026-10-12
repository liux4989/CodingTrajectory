# Tool classification from transcript evidence

Tool views now use generic identity categorization for every tool. Predefined
semantic mappings, shell-content classification, and tool-specific display
profiles are disabled in this path. No name, input key, or result prose is used
to infer a file, search, edit, command, or coordination display category.

## JSONL characteristics

The existing adapters normalize provider-specific records into a tool call and
a linked result. No new provider parser is needed for the classification rule.

| Provider | Call identity and input | Result linkage and evidence |
| --- | --- | --- |
| Claude Code | `message.content[].type=tool_use`, `id`, `name`, `input` | `tool_result.tool_use_id`, `content`, optional `toolUseResult` |
| Codex | `payload.type=function_call`, `call_id`, `name`, JSON `arguments`; or `custom_tool_call` with `input` | Matching `function_call_output` or `custom_tool_call_output`, `call_id`, `output`; explicit native events can supply lifecycle evidence |
| Pi | Assistant `toolCall`, `id`, `name`, `arguments` | `role=toolResult`, `toolCallId`, `toolName`, `isError`, `content` |
| Amp | Message `tool_use`, `id`, `name`, `input`; live `tool.call` hooks | Message `tool_result.toolUseID`; reconciled `tool.result` hooks and result status |

Names identify operations. Call IDs join lifecycles, not categories. Arguments
and outputs supply content measurements; literal input arguments also supply
bounded operation details. Result
status supplies an outcome independently of classification. A wrapper completion
must not invent success for a reconstructed child invocation.

The two local sessions that prompted this change provide a useful structural
cross-check. At initial inspection, the Claude source had four calls and four
linked results; the Codex source had five custom calls and one function call,
each with a matching result. These are inspected prefixes of growing live logs.
Adapters may additionally reconstruct native child activities. Those projections
do not represent additional provider-visible content.

## Generic algorithm

1. Normalize source records through the existing adapter. Keep the recorded name,
   call/result identity, status, timing, measurements, and projection provenance.
2. Use the recorded canonical tool name for every tool. Familiar and unfamiliar
   tools follow the same rule: `bash` stays `bash`, `Read` stays `Read`, and a
   provider-qualified plugin name stays that identity. Never infer `ReadFile`
   from a shell command containing `cat` or `SearchText` from a `query` key.
3. If the name is unavailable, retain a structural label from the canonical
   item kind, such as `Tool call`, without inventing the identity.
4. Extract details from arguments independently of the tool name. Canonical
   command items preserve command text, quoting, pipelines, script line breaks,
   and an explicitly supplied working directory. Other arguments are traversed
   as bounded key/value paths, including nested query arrays and URLs. Text
   arguments precede envelope enums and numeric transport controls. Credential
   fields are redacted and URL credentials/query strings removed before display.
   Keep the activity visible even when its description is unavailable. Hide only
   explicitly suppressed adapter evidence or known control-only cells, such as
   an expanded wrapper already represented by children or empty terminal polls.
   All calls remain separate overview rows: identical names do not establish
   that two calls performed the same action.
5. Put all tool content under `Tool calls`, with children grouped by exact identity.
   Child keys derive from the complete name; provider-qualified display labels
   lead with the operation so bounded tables distinguish different operations.
6. Measure canonical tool arguments plus results once. Projected children share
   the parent's measurement and usage allocation; category children partition
   their parent rather than adding another token contribution. Existing provider
   input anchoring still scales visible estimates; it is not direct billed usage.

The former `Files`, `Command output`, `Code changes`, `Coordination`, and `Other
tool output` display buckets are disabled. Starting context, user input, and
assistant messages remain structural transcript categories. The provider
adapters still reconstruct native item kinds, lifecycle, and topology; this
preserves evidence independently of display categorization. Shared legacy
registries may still serve these structural consumers, but tool summaries no
longer call the semantic classifier or select its optimization profiles.

## Retained sessions

Compact summaries retain `classification: identity` separately from
`activity_hidden_reason`. On reading old summaries, the canonical name replaces
old semantic labels. Old semantic descriptions and optimization profiles are
dropped, while new `detail_basis: arguments` previews survive body-free retention.
Already-retained items without either source arguments or an argument preview
cannot recover discarded details; no description is invented for them.
Hidden flags caused by the former vocabulary allowlist are removed. Explicit suppression,
including retained expanded-wrapper markers and wrapper provenance, remains
effective. Suppression is normalized before summaries are retained again.

## Qualification

Run the full committed metrics workflow and its quality-gate wrapper. The Pi
baseline's intentionally changed overview expectations are derived from source
call/result pairs in its audit; token, runtime, and cost expectations are unchanged.
Local overview and stats checks exercise both supplied sessions. These checks
qualify the implemented adapters and available evidence, not all possible
provider formats or arbitrary semantic inference.

An additional in-memory adapter qualification covered original names, two
unfamiliar identities (`future_action_2040`, `mcp__unseen_namespace__operation`),
and familiar names (`ReadFile`, `bash`) across Claude, Codex, Pi, and Amp source
shapes. All 60 combinations of provider, identity selection, and
full/measurements/retained representation preserved calls and matched context
compositions and overview rows. Summaries always used identity classification
and contained no tool-specific optimization profiles, including
when the recorded name happened to match a former predefined concept.
The existing Amp integration qualification also passed lifecycle reconstruction,
deduplication, retained replay, and public API checks. No unit tests were added.

The argument-detail follow-up additionally qualified the supplied Codex session
`01a12eb5-0efa-7d80-88ec-64bdce718cc3`: the search at item `388e9db4` retains both
query-array entries, and the command at `0e66c794` retains the quoted `rg` pattern,
file argument, pipeline, and explicit working directory. Script previews retain
line boundaries as literal `\n` markers. Overview uses a bounded target preview;
item retrieval exposes the retained detail without terminal-line clipping.
Claude, Codex, and Pi detail/context parity and replay idempotence passed across
full, measurements, and retained representations, including contract-validated
overview, item, stats, and summary requests. Terminal previews preserve item
references when a long argument description is clipped.

## Script executions between turns and items

The canonical hierarchy is `Turn → execution unit → item references`.
`Turn.execution_units` contains direct item references and tool-script executions.
Existing flat `Turn.items` retain evidence and measurements once. Scripts own
references to their invocation and child items, not a reconstructed action
sequence; promises, loops, branches, and transformations remain in full source.

Existing commands consume this shared layer:

```sh
ct session overview SESSION_ID
ct session items SESSION_ID ITEM_ID
ct session events SESSION_ID --item ITEM_ID
ct session stats SESSION_ID --output json
ct session summary SESSION_ID
ct session search SESSION_ID QUERY
ct session usage SESSION_ID --output json
```

Overview places script units within each turn, with an **Open source log** link and
observed composition beneath the script. Items/events/search/tool usage carry
`script_id` references. Their `execution_units` response layer resolves the
parents for returned evidence, including when an item page omits its parent.
Summary recent activity uses the same script units. Stats partition script-owned
content under `Tool scripts`; usage preserves the original item cost ledger and
provider totals. Source-only candidates and bounded host inventories never mint
additional content or billed usage. Direct Claude tools and individual executors
continue to work through this hierarchy.

Each script has four parts:

- **Source:** a reference to the original log with record indices and digests,
  source field, content digest, and provider identity. The Open source log link
  opens that log; it does not point to an extracted script. Core responses
  contain no raw code body. Native shell
  text comes from the actual shell argument, preserving whitespace that command
  previews may normalize. An argv vector is not rewritten into invented code.
- **External calls:** observed native calls and explicit host inventories
  (Pi `nestedCalls`, Codex originating-call metadata) retain identities, bounded
  argument previews, full argument references where recorded, outcomes, timings,
  and result references. `ownership_basis` separately identifies inferred
  ownership of an observed call. Source-only adapter candidates carry no runtime
  outcome or timing, and their arguments are marked `source_reconstructed`.
- **Context references:** owning turn, request, adjacent items, and preceding and
  following assistant messages. Context bodies remain on surrounding items;
  following messages are not presumed to interpret results.
- **Result:** emitted-output reference, invocation status/timing, lifecycle
  coverage, and failure-output reference. A returning orchestration invocation
  alone does not establish runtime completion.

`inventory_coverage` records the host's completeness claim. Without inventory
proof it remains unknown, even when the observed-call list is empty. Source-only
candidates are not exhaustive code analysis; agents read full source for deeper
understanding. No invocation list claims to explain program control flow.

Source, argument, and output evidence stays in the original session logs.
Execution-layer construction reads those logs and creates no exported source,
provenance, argument, or result files. References identify the selected log
snapshot and records; record digests allow evidence checks after a live log
appends. Native item/event identities locate arguments, and inventory pointers
locate host-recorded arguments. Missing literal source stays a direct tool item;
no code is invented. Full evidence remains available while the original logs
are retained. Previously generated files under `.artifacts/tool-sources/` or
the user cache are no longer used and can be removed manually.
There are no separate source or script inspection commands.
