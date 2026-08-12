"""Task prompt templates for each task type and tool variant.

Prompt = tool_variant_preamble (how to explore) + task_instruction (what to analyze).

- Tool variant preamble: describes available tooling, common across all tasks.
- Task instruction: describes the analysis goal, common across all tool variants.
"""
from __future__ import annotations

from .models import TaskType, ToolVariant


# ---------------------------------------------------------------------------
# Tool variant preambles — task-agnostic, describe *how* to explore the log
# ---------------------------------------------------------------------------

_VARIANT_PREAMBLES: dict[ToolVariant, str] = {
    ToolVariant.CT_CLI: """\
You have the `ct` CLI for exploring coding agent session logs. Use the JSON output forms shown below.

The log file to analyze is at: {log_file}

## Structured View
Use this sequence to explore the log through enriched, post-processed structure:
1. `ct project list --output json`  → list all known projects; find the project for the log above
2. `ct project sessions <PROJECT_NAME> --output json`  → list sessions for the project and choose a session ID
3. `ct session overview <SESSION_ID> --output json`  → session structure, item types, user requests — start here
4. `ct session items <SESSION_ID> [<ITEM_ID> ...] --output json`  → full detail for one session or specific items (returns JSON array)

## Raw View
Only fall back to raw events when structured data is truncated or missing detail you need:
1. `ct session events <SESSION_ID> --type TYPE [--filter KEY=VALUE ...] --output json`
2. `ct session events --event-id <EVENT_ID> [--event-id <EVENT_ID> ...] --output json`

""",

    ToolVariant.TOOL_ASSISTED: """\
The coding agent log file is at: {log_file}

## Log format
Format: JSONL — each line is a JSON object representing one event from the coding agent.
Common fields in each line vary by vendor but typically include:
  - type/event_type: the kind of event (user message, tool call, llm response, etc.)
  - timestamp: when the event occurred
  - content/message/text: the payload
  - tool_name, tool_input, tool_output: for tool call events
  - session_id, parent_session_id: for session relationships

## Vendor-specific patterns
- Claude Code: events keyed by `type` (e.g. "assistant", "tool_use", "tool_result"), session files in ~/.claude/projects/
- Codex CLI: events with `type` field (e.g. "message", "function_call"), thread-based

Use your available tools (Read, Grep, Bash, etc.) to explore the file.

## Strategy
Sample the first ~50 lines to identify the vendor and schema, then read selectively.
""",

}


# ---------------------------------------------------------------------------
# Task instructions — variant-agnostic, describe *what* to analyze
# ---------------------------------------------------------------------------

_TASK_INSTRUCTIONS: dict[TaskType, str] = {
    TaskType.SESSION_COMPACT: """\
Given this coding-agent session log, generate a compact continuation summary that preserves the minimum sufficient state needed to continue the current task after context compression.

Include whichever of the following sections are relevant to the log (omit any that do not apply):
- **Task**: the original request
- **Progress**: what is done, what is in progress, and what is blocked (use a checklist format)
- **Key decisions**: choices made and why (includes tradeoffs, pivots, rejected approaches)
- **Critical context**: facts, discoveries, or environmental details needed to continue
- **User constraints**: explicit preferences, boundaries, or requirements from the user
- **Next steps**: what should happen next to move the task forward
- **Files touched**: list files that were read or modified""",

    TaskType.ERROR_ROOT_CAUSE: """\
The agent encountered one or more error-retry loops during this session. Analyze:
- **Location**: Which item(s) contain the error loop? Identify by item ID or position.
- **Root cause**: What was the underlying cause of the failure (not just the error message)?
- **Retry count**: How many times did the agent retry before resolving (or giving up)?
- **Resolution**: What change finally resolved the error? Or did the agent give up / work around it?
- **Impact**: How much of the session was spent in error recovery vs. productive work?

Start with the high-level session overview to locate error clusters, then drill into specific steps for details.""",

    TaskType.SUBAGENT_DELEGATION: """\
Analyze the subagent delegation pattern in this coding trajectory:
- **Delegation inventory**: For each subagent spawned, what task was assigned and what was the parent's intent?
- **Return analysis**: What did each subagent return to its parent? Was the result used correctly?
- **Context fidelity**: Did any delegation lose important context (constraints, decisions, file state) in the handoff?
- **Failure handling**: If a subagent failed, how did the parent respond (retry, delegate elsewhere, handle inline)?
- **Topology**: Draw the delegation graph showing parent→child relationships and data flow direction.""",

    TaskType.TOOL_FAILURE_REPORT: """\
Extract every distinct tool invocation that failed during this session. For each failure report:
- **Tool name**: Which tool was called?
- **Input summary**: What was the tool asked to do (brief)?
- **Error**: The actual error message or failure mode.
- **Recovery**: Did the agent recover? How (retry with different input, alternative tool, skip)?
- **Classification**: Categorize as: transient (retry fixed it), permanent (agent worked around it), or fatal (blocked progress).

Only report actual tool failures — not lifecycle events, not successful calls, not internal framework messages.
Ignore duplicate retries of the same failure; group them as one entry with a retry count.""",
}


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_prompt(task_type: TaskType, tool_variant: ToolVariant, log_file: str) -> str:
    """Build the full task prompt = tool preamble + task instruction.

    The two halves are independent:
    - preamble varies by tool_variant only (how to explore the log)
    - instruction varies by task_type only (what analysis to produce)
    """
    preamble = _VARIANT_PREAMBLES[tool_variant].replace("{log_file}", log_file)
    instruction = _TASK_INSTRUCTIONS[task_type]
    return f"{preamble}\n--- Task Instructions ---\n{instruction}"
