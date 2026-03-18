# CLI Design

## Goal

The CLI has one job: help an LLM analyze coding-agent logs without reading
the full raw log stream first.

Three goals:

1. Progressive disclosure — read the big picture first, drill into details only when needed.
2. Contextual understanding — return enriched, post-processed structure instead of raw events.
3. Noise removal — suppress execution-recording noise by default.

## Design Principle

- navigation tree for orientation
- evidence of atomic action for detail

## Public Surface

```
ct [--output FILE] [--pretty] project list [--agent-vendor AGENT_VENDOR]
ct [--output FILE] [--pretty] project <project_name> [--agent-vendor AGENT_VENDOR]
ct [--output FILE] [--pretty] project --logfile PATH
ct [--output FILE] [--pretty] trajectory overview <trajectory_id> [--logfile PATH]
ct [--output FILE] [--pretty] trajectory scan <trajectory_id> [--logfile PATH] --type TYPE [--filter KEY=VALUE ...]
ct [--output FILE] [--pretty] step details <step_id>
ct [--output FILE] [--pretty] event detail <event_id>
```

Global options:

- `--output FILE` / `-o FILE` — write JSON output to a file instead of stdout
- `--pretty` — pretty-print JSON output

Subcommand options:

- `project --logfile PATH` — load a specific log file and return its trajectory id
- `project --agent-vendor AGENT_VENDOR` — filter projects or trajectories by agent vendor. Supported values:
  - `claude_code` — Anthropic Claude Code CLI
  - `codex_cli` — OpenAI Codex CLI
  - `gemini_cli` — Google Gemini CLI
  - `amp` — Amp agent
- `trajectory ... --logfile PATH` — analyze a specific log file instead of resolving by trajectory id

Everything else (session, turn, event, raw) is not exposed at this stage.

## Intended Reading Flow

1. `project list` — find project names
2. `project <project_name>` — list trajectories for a project, get the trajectory id
   - or `project --logfile PATH` — load a log file directly and get the trajectory id
3. `trajectory overview <trajectory_id>` — read the navigation tree, identify relevant steps
4. `trajectory scan <trajectory_id> --type TYPE [--filter ...]` — cross-cut the tree to find all steps of a type, optionally narrowed by shape predicates
5. `step details <step_id>` — read the evidence for a specific step
6. `event detail <event_id>` — resolve the full content of a `$truncated` field from step details or scan

---

## `trajectory overview <trajectory_id>` — Navigation Tree

Purpose:
Return the whole session tree in lightweight form. Each step is a navigation
node: just enough to orient, never enough to overwhelm.

Shape:

- trajectory identity
- nested sessions with connection context
- nested turns with user request
- nested steps with type only

Example:

```json
{
  "trajectory_id": "t1",
  "sessions": [
    {
      "session_id": "s1",
      "connection": { "role": "main" },
      "turns": [
        {
          "turn_id": "turn-1",
          "user_request": "analyze the schema design",
          "steps": [
            { "step_id": "step-1", "type": "tool_call" },
            { "step_id": "step-2", "type": "assistant_response" }
          ]
        }
      ]
    },
    {
      "session_id": "s2",
      "connection": { "relationship": "spawned_subagent", "parent_session_id": "s1" },
      "turns": []
    }
  ]
}
```

Rules:

- keep it hierarchical and lightweight
- steps show type only — no content, no event refs
- connection context captures session relationships, not operation detail

---

## `trajectory scan <trajectory_id>` — Cross-Cut Query

Purpose:
Flatten the full trajectory tree and return all steps of a given type.
Each `--filter` expression narrows the result set further (AND logic), applied
against the step's `shape` fields.

Options:

- `--type TYPE` — required. Step type selector: `tool_call`, `assistant_response`, `plan_subagent`, `todo_list`, `session_handoff`
- `--filter KEY=VALUE` — exact match on a shape field. Chainable.
- `--filter KEY=*` — field exists and is not null.
- `--filter KEY=!` — field absent or null.

Keys support dot-path notation into nested shape fields (e.g. `tool_output.error`).

Examples:

```bash
# All tool_call steps
trajectory scan t1 --type tool_call

# All Read calls
trajectory scan t1 --type tool_call --filter tool_name=Read

# All tool_call steps where tool_output has an error field
trajectory scan t1 --type tool_call --filter tool_output.error=*

# Chain: Read calls that returned an error
trajectory scan t1 --type tool_call --filter tool_name=Read --filter tool_output.error=*
```

Shape:

```json
{
  "trajectory_id": "t1",
  "type": "tool_call",
  "matches": [
    {
      "step_id": "step-3",
      "session_id": "s1",
      "turn_id": "turn-2",
      "user_request": "analyze the schema",
      "shape": {
        "tool_name": "Read",
        "tool_input": { "file_path": "/src/foo.py" },
        "tool_output": { "content": "..." }
      }
    }
  ]
}
```

Rules:

- `--type` is required — scan is always type-scoped
- Full `shape` is returned for each match (unlike `trajectory overview`)
- Use `step details <step_id>` if you need `event_ids` or `operations`

---

## `step details <step_id>` — Evidence of Atomic Action

Purpose:
Return the full enriched step. Shape is type-specific.

Shape:

- step identity and type
- operations — what actions were performed
- shape — type-specific enriched fields
- event_ids — anchors to underlying raw events

### tool_call

```json
{
  "step_id": "step-1",
  "type": "tool_call",
  "operations": ["Read"],
  "shape": {
    "tool_name": "Read",
    "tool_input": { "file_path": "/src/foo.py" },
    "tool_output": { "content": "..." }
  },
  "event_ids": ["e1", "e2"]
}
```

### plan_subagent

```json
{
  "step_id": "step-3",
  "type": "plan_subagent",
  "operations": ["spawn", "collect_result"],
  "shape": {
    "agent_input": { "prompt": "..." },
    "agent_output": { "result": "..." },
    "agent_session_id": "s2"
  },
  "event_ids": ["e5", "e6", "e7"]
}
```

### assistant_response

```json
{
  "step_id": "step-2",
  "type": "assistant_response",
  "operations": ["text_reply"],
  "shape": {
    "text": "Here is the analysis...",
    "stop_reason": "end_turn",
    "usage": { "input_tokens": 120, "output_tokens": 340 }
  },
  "event_ids": ["e3", "e4"]
}
```

Rules:

- shape fields are determined by type — no generic fallback
- string fields longer than 500 chars are replaced with a `$truncated` ref object; use `event detail <event_id>` to get full content
- event_ids are present as anchors for both the step and individual truncated fields

---

## `event detail <event_id>` — Full Event Content

Purpose:
Resolve the full payload of a single event. Primarily used to expand
`$truncated` fields returned by `step details` or `trajectory scan`.

A `$truncated` field looks like:

```json
"tool_output": {
  "$truncated": true,
  "preview": "first 500 chars…",
  "event_ids": ["<uuid>"]
}
```

Call `event detail <uuid>` on any of the listed `event_ids` to get the
complete event payload including the untruncated field value.

Shape: see `serialize_event_detail` — event identity, type, timestamp, and
one of `tool_call`, `llm`, or `text` depending on event type.

Rules:

- event_ids in a `$truncated` ref point to the events that carry the full value
- `event detail` also accepts any `event_id` from `step details` top-level `event_ids`

---

## What The CLI Hides

These are not exposed in the public surface:

- session overview — session boundaries are visible inside trajectory overview
- turn overview — turn boundaries are visible inside trajectory overview
- event get — event_ids are present in step details as anchors
- raw — source proof surface, not yet exposed

They remain in the underlying RPC layer but are not public CLI commands.

## Most Important Fields

- stable ids (trajectory_id, session_id, turn_id, step_id)
- session connection context
- user request text
- step type
- step operations and shape
- event_ids (in step details only)
