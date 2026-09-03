# Codex Activity Reconstruction

## Reference behavior

CT's activity-cell state machine follows the Codex TUI implementation in
`codex-rs/tui/src/exec_cell/model.rs` and its execution-flow coverage in
`codex-rs/tui/src/chatwidget/tests/exec_flow.rs`. Its ThreadItem replay follows
`chatwidget/replay.rs`, `chatwidget/command_lifecycle.rs`,
`chatwidget/tool_lifecycle.rs`, and `history_cell/plans.rs`: commands,
web-searches, file changes, plan updates, and collaboration calls are distinct
history cells rather than variants of a generic shell command.

- A cell groups only contiguous compatible commands.
- It has a hard cap of 32 commands.
- Only agent-originated and unified-exec-startup commands are groupable.
- A command must have a successful completion (`exit_code == 0`) before it can
  join the compact `Ran N commands` presentation.
- Messages, failures, user-shell commands, web activity, mutations, and other
  incompatible tool forms flush the active cell.

Codex TUI receives those native command lifecycles while the session is live.
CT instead reconstructs a completed historical JSONL file, so it preserves a
separate evidence and canonical-lifecycle layer before projecting the compact
cell.

## Historical Codex paths

1. `event_msg.item_started` / `item_completed` carrying a Codex ThreadItem is
   the exact path. CT runs every terminal `item_completed.item.type` through a
   native-item decoder: established content/runtime types retain their
   dedicated paths, and tool-shaped types normalize to canonical actions with
   input, output, status, timing, and native provenance. This includes the
   historical `Extension(kind="web.search")` spelling, whose action/query and
   result cards are an observed web result—not an unknown `exec` child.
   `CommandExecution`, `FileChange`, `WebSearch`, `Plan`, and
   `CollabAgentToolCall` retain their specialized mappings. Eligible adjacent
   command successes use the same 32-item cell state machine as the TUI.
2. Older JSONL may retain only a `custom_tool_call(name="exec")` JavaScript
   wrapper. CT recognizes direct literal calls to `tools.exec_command`,
   `tools.web__run` search/image-query and browse operations,
   `tools.apply_patch`, `tools.update_plan`, and known collaboration methods,
   plus a literal `await Promise.all([...])` list of those calls. To discover
   actions it does not evaluate JavaScript or follow aliases/control flow; an
   unknown web operation or direct `tools.*` reference keeps the raw wrapper
   visible. Once every direct tool reference has been recognized, an opaque
   display tail is ignored.
3. A native terminal item binds to the single matching open static wrapper
   child only when turn scope, time ordering, and the action-specific input
   agree unambiguously. Native data wins: for example, `FileChange` contributes
   its real changed paths and result rather than a guessed patch summary. If an
   older source has no native child, CT emits a typed `derived_static` item as
   a fallback.
4. Wrapper completion never proves a nested command exit code or generic tool
   result. Static children therefore retain `fidelity=derived_static` evidence
   unless one lexically known nested action has an explicit persisted wrapper
   result payload. Public projections omit an outcome for those children;
   Codex has no `unknown` command status. The `Script completed` / `Script
   failed` banner alone is never nested-outcome evidence; only the wrapper
   itself can be marked failed from it. A JavaScript syntax error remains a
   visible failed `exec`, because no nested action could have started.
5. The raw `exec` wrapper remains canonical evidence. It is hidden from
   semantic activity projections when every nested activity was safely
   reconstructed or bound to native or explicit wrapper-result evidence;
   unresolved or unsupported wrappers remain visible as `exec`. Overview and
   summary recent activity consume the same cell projector, so a superseded
   transport wrapper cannot disappear from one view and reappear in another.
   Summary excludes its own `session.summary` / `session.search` commands at
   the projector boundary to avoid recursively reporting retrieval activity.
Empty `write_stdin` calls are background-terminal polls, not shell commands.
Contiguous polls for the same namespaced terminal identity become one wait
cell while retaining every canonical item reference. That cell is control-only
evidence and is omitted from default overview and summary projections; it
remains available through canonical item/detail evidence. Non-empty stdin is a
separate, visible terminal interaction because it changes terminal state, and
its raw input remains available only through the item evidence layer. Like
Codex's `TerminalInteraction` notification, neither form has an execution
outcome; absence is not rendered as `unknown`. The enclosing JavaScript
wrapper's completion remains separate and never claims that the underlying
process completed.

Measurements retention keeps only bounded compact markers for wrapper
suppression and terminal grouping. Those markers contain no command body,
stdin content, or process/session identifier; a content-free assistant-output
epoch preserves wait-streak boundaries after compact retention drops text.

Consequently a legacy session can show `Searched the web for …`, `Updated plan:
4 item(s)`, or an ungrouped `RunCommand: rg` rather than a raw `exec` code cell.
It carries no displayed outcome when only static evidence exists. A newer
session with native exit-zero facts can show `Ran N commands`.

## Physical session segments

Codex can persist a resumed thread into a new rollout JSONL while retaining the
same session ID. Discovery retains every matching source path, orders the
segments by their observed start time, and collapses them into one canonical
session before graph assembly. They are source evidence for one session, not
duplicate graph nodes or a parent/child relationship. This ensures that an
overview reconstructs the complete historical thread rather than only its last
rollout file.

## Cross-agent contract

The shared projector does not require Codex-specific payloads. Pi `bash` and
Claude Code `Bash` calls become canonical `command_execution` items, and their
direct successful tool lifecycles are eligible for the same contiguous command
cell. Adapters own the evidence mapping; the presentation state machine is
vendor-neutral.
