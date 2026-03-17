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
ct [--log-file PATH] [--output FILE] [--pretty] list
ct [--log-file PATH] [--output FILE] [--pretty] trajectory overview <trajectory_id>
ct [--log-file PATH] [--output FILE] [--pretty] trajectory scan <trajectory_id> --type TYPE [--filter KEY=VALUE ...]
ct [--log-file PATH] [--output FILE] [--pretty] step details <step_id>
```

Global options:

- `--log-file PATH` — analyze a specific log file instead of auto-discovery
- `--output FILE` / `-o FILE` — write JSON output to a file instead of stdout
- `--pretty` — pretty-print JSON output

Everything else (session, turn, event, raw) is not exposed at this stage.

## Intended Reading Flow

1. `list` — find the trajectory id
2. `trajectory overview <trajectory_id>` — read the navigation tree, identify relevant steps
3. `trajectory scan <trajectory_id> --type TYPE [--filter ...]` — cross-cut the tree to find all steps of a type, optionally narrowed by shape predicates
4. `step details <step_id>` — read the evidence for a specific step

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
- event_ids are present as anchors but not resolvable via public CLI at this stage
- full content, no truncation

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
