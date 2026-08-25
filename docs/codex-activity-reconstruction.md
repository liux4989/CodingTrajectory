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
   the exact path. CT directly reconstructs `CommandExecution`, `FileChange`,
   `WebSearch`, `Plan`, and `CollabAgentToolCall` into their corresponding CT
   items. Eligible adjacent command successes use the same 32-item cell state
   machine as the TUI.
2. Older JSONL may retain only a `custom_tool_call(name="exec")` JavaScript
   wrapper. CT recognizes direct literal calls to `tools.exec_command`,
   `tools.web__run` search/image-query and browse operations,
   `tools.apply_patch`, `tools.update_plan`, and known collaboration methods,
   plus a literal `await Promise.all([...])` list of those calls. To discover
   actions it does not evaluate JavaScript or follow aliases/control flow; an
   unknown web operation or direct `tools.*` reference keeps the raw wrapper
   visible. Once every direct tool reference has been recognized, an opaque
   display tail is ignored.
3. A native item binds to the single matching open static wrapper child when
   the evidence is unambiguous. Native data wins: for example, `FileChange`
   contributes its real changed paths and result rather than a guessed patch
   summary. If an older source has no native child, CT emits a typed
   `derived_static` item instead.
4. Wrapper completion never proves a nested command exit code or generic tool
   result. Static children therefore retain `outcome=unknown`; only native
   evidence can make a command eligible for `Ran N commands`.
5. The raw `exec` wrapper remains canonical evidence. It is hidden only from
   the compact activity view when every nested activity was safely
   reconstructed or bound to native evidence; unresolved or unsupported
   wrappers remain visible as `exec`.

Consequently a legacy session can show `Searched the web for …` or `Updated
plan: 4 item(s)` with its nested outcome marked unavailable, rather than a raw
`exec` code cell. A legacy command remains `RunCommand: rg [outcome
unavailable]` instead of a misleading `Ran N commands`. A newer session with
native exit-zero facts shows `Ran N commands`.

## Cross-agent contract

The shared projector does not require Codex-specific payloads. Pi `bash` and
Claude Code `Bash` calls become canonical `command_execution` items, and their
direct successful tool lifecycles are eligible for the same contiguous command
cell. Adapters own the evidence mapping; the presentation state machine is
vendor-neutral.
