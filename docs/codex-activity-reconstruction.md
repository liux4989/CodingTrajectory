# Codex Activity Reconstruction

## Reference behavior

CT's activity-cell state machine follows the Codex TUI implementation in
`codex-rs/tui/src/exec_cell/model.rs` and its execution-flow coverage in
`codex-rs/tui/src/chatwidget/tests/exec_flow.rs`.

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

1. `event_msg.item_started` / `item_completed` with `CommandExecution` is the
   exact path. CT records the command, its native source, exit code, and
   completion outcome. Eligible adjacent successes use the same 32-item cell
   state machine as the TUI.
2. Older JSONL may retain only a `custom_tool_call(name="exec")` JavaScript
   wrapper. CT recognizes an unconditional, literal
   `await tools.exec_command({cmd: ...})` without evaluating JavaScript and
   creates a `derived_static` command item linked to the raw wrapper call.
   It never treats wrapper completion as child-command success: a wrapper can
   complete after discarding a non-zero child exit code.
3. The raw `exec` wrapper remains canonical evidence. It is hidden only from
   the compact activity view when a derived/native child activity replaced it;
   unresolved or unsupported wrappers remain visible as `exec`.

Consequently a legacy session can show `RunCommand: rg [outcome unavailable]`
instead of a misleading `Ran N commands`. A newer session with native exit-zero
facts shows `Ran N commands`.

## Cross-agent contract

The shared projector does not require Codex-specific payloads. Pi `bash` and
Claude Code `Bash` calls become canonical `command_execution` items, and their
direct successful tool lifecycles are eligible for the same contiguous command
cell. Adapters own the evidence mapping; the presentation state machine is
vendor-neutral.
