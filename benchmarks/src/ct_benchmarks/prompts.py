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
You have the `ct` CLI for exploring coding agent session logs. All commands return JSON.

Run `ct --help` to see the full workflow, available commands, and their usage.

The log file to analyze is: {log_file}
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
- Gemini CLI: events in session JSONL files
- Amp: thread-based, events with `type` field, parent_thread_id for subagents

Use your available tools (Read, Grep, Bash, etc.) to explore the file.
""",

    ToolVariant.READ_FILE: """\
Analyze the coding agent log file at: {log_file}
""",
}


# ---------------------------------------------------------------------------
# Task instructions — variant-agnostic, describe *what* to analyze
# ---------------------------------------------------------------------------

_TASK_INSTRUCTIONS: dict[TaskType, str] = {
    TaskType.SESSION_SUMMARY: """\
Summarize this coding session log. Cover:
- What was the user trying to accomplish?
- What approach did the agent take?
- What tools were used and in what sequence?
- What was the final outcome?
Provide a concise but comprehensive summary.""",

    TaskType.SESSION_CONNECTION: """\
Analyze the session connection map of this coding trajectory. Answer:
- How many sessions exist and how are they related (parent/child, subagent, handoff)?
- What is the overall task status in the big picture?
- Draw the session topology (linear, branching, etc.)
- What role does each session play?""",

    TaskType.EFFORT_DISTRIBUTION: """\
Analyze the effort distribution in this coding session. Break down into:
- Planning: time/steps spent on planning, reading docs, understanding requirements
- Executing: time/steps spent on actual code writing, file edits
- Bug fixing: time/steps spent on debugging, error resolution, retries
- Interactive refinement: time/steps spent on user feedback loops, iterations
Provide counts and percentages for each category.""",
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
    return f"{preamble}\n--- Task ---\n{instruction}"
