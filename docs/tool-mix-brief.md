# Product brief: Tool mix & context

Status: implemented in Loop Investigation (`packages/plugins/loop/web/src/tool-mix.tsx`).
The standalone `breakdown` plugin was retired. Owner dogfooding is pending.

## Problem

Developers who run coding agents daily cannot see where a session's context and
runtime went. `ct session stats` reports context composition, but not which
kinds of tool calls produced it or which individual calls dominated. So users
cannot tell whether a prompt, `AGENTS.md`, or tool setup change would help.

## User and question

- **User:** a developer reviewing their own recent agent sessions.
- **Question:** "Where did this session's context and time go, and which calls
  dominated?"
- **Decision it informs:** change instructions or tooling. For example, "file
  reads go through `sed` in the shell instead of a read tool", or "test command
  output fills most of the context".

## Scope

A **Tool mix & context** panel inside Loop Investigation (Analytics), not a
separate plugin or screen.

1. **Tool mix:** call counts, visible-token estimates, and measured durations
   grouped by Core activity concept (`ReadFile`, `EditFile`, `WriteFile`,
   `RunCommand`, `SearchText`/`ListFiles`, `WebFetch`/`WebSearch`,
   `SubagentTask`, other). The panel may collapse concepts for display but
   never classifies tool names itself.
2. **Top calls:** the calls with the most visible tokens and the longest
   measured durations, each linked to its `CanonicalReference`.
3. **Sequence:** the ordered call strip, filterable by group; each call opens
   its item in the Investigation evidence pane.
4. **Context composition:** the existing `session.stats` categories, shown next
   to the tool mix.

Comparison belongs to Loop Reports, for a meaningful cohort only: runs of the
same task, or one project before and after an instruction change. Comparing
arbitrary sessions is out of scope.

## Non-goals

- Relevance, redundancy, waste, or pass/fail scores. These are judgments for a
  later enrichment layer (Monitor/Improve), not Analytics.
- Presenting estimates as billed tokens or cost. Estimates stay labelled;
  unavailable provider values stay `—`.
- Splitting a multi-command shell wrapper into separate calls.

## Core evidence

No Core change was needed:

- Adapters mark shell calls as `command_execution` items, which Core classifies
  with `classify_shell` regardless of the provider's tool name. Amp's
  `shell_command` is therefore already classified like other shells.
- `session.items` v6 returns each item's Core concept (`detail.concept`),
  target, estimated content tokens (`measurements`), and measured duration
  (`output_evidence.duration_ms`). The panel reads tool-shaped items with the
  `types` filter, 1,000 per page, up to 5,000 calls, and labels truncation.

"Via shell" counts `command_execution` items that Core classified as something
other than a command, such as a `sed` or `cat` read.

## Success criteria

Dogfood on the owner's 20 most recent local sessions across at least two agent
vendors, and record:

- at least three concrete, actionable findings a user would not have seen in
  `ct session stats`, each with evidence links; or an explicit statement that
  the panel did not surface any, which means the feature is not ready;
- every bar and row drilling down to the contributing item;
- no metric shown without its source, and no estimate labelled as billed usage.

## Migration

Done: the tool mix, sequence strip, top calls, and composition view live in
Loop Investigation; `packages/plugins/breakdown` and its `docs/plugin.md`
section were removed.
