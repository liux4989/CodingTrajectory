# CLI Draft

## Goal

The CLI is for one job: help an LLM analyze coding-agent logs without reading
the full raw log stream first.

It has three goals:

1. Progressive disclosure
   The LLM should read the big picture first, then drill into details only when
   needed.
2. Contextual understanding
   The CLI should return enriched, categorized, post-processed structure instead
   of forcing the LLM to infer everything from raw events.
3. Noise removal
   The CLI should hide non-session analysis noise by default, especially raw
   execution-recording logs that do not help reconstruct the coding session.

## Principle
  - structure for navigation
  - atomic events for evidence
  - derived queries later for scalable analysis
  

## Design Direction

The CLI should expose:

- `overview` for composite resources such as trajectory, session, turn, and step
- `get` only for atomic events

This lets the LLM:

- start from a tree
- expand only the relevant node
- drill into exact evidence only at the event layer
- avoid context pollution from low-value raw logs

The CLI itself should stay thin:

- overview logic belongs in the core overview / enrichment layer
- the CLI should mostly map commands to RPC methods
- overview responses must preserve stable ref ids for further drill-down

## Command Pattern

Each layer should follow the same pattern:

- `trajectory overview <trajectory_id>`
- `session overview <session_id>`
- `turn overview <turn_id>`
- `step overview <step_id>`
- `event get <event_id>`
- `raw <event_id>`

`overview` is the main reading surface.
`event get` is the exact inspection surface.
`raw` is the proof surface.

## Trajectory

### `trajectory overview <trajectory_id>`

Purpose:
Return the whole reconstructed trajectory tree in a lightweight form.

Shape:

- trajectory identity and typed context
- nested sessions
- nested turns
- nested user request
- nested steps
- event references

Example:

```json
{
  "trajectory": {
    "trajectory_id": "t1",
    "context": {
      "multi_agent": true,
      "relationship_types": ["spawned_subagent"]
    },
    "session_count": 2,
    "turn_count": 5
  },
  "tree": [
    {
      "session_id": "s1",
      "context": {
        "role": "main"
      },
      "turns": [
        {
          "turn_id": "turn-1",
          "user_request": "analyze the schema design",
          "steps": [
            {
              "step_id": "step-1",
              "event_refs": [
                {
                  "event_id": "e1",
                  "type": "user.prompt.submitted",
                  "category": "user_interaction"
                },
                {
                  "event_id": "e2",
                  "type": "tool.call.requested",
                  "category": "tool_call"
                }
              ]
            }
          ]
        }
      ]
    },
    {
      "session_id": "s2",
      "context": {
        "relationship": "spawned_subagent"
      },
      "turns": []
    }
  ]
}
```

Rules:

- keep it hierarchical
- keep it lightweight
- include typed context, not invented summaries
- include event refs, not full event bodies

## Session

### `session overview <session_id>`

Purpose:
Return one session in analysis form.

Shape:

- canonical session ids and metadata
- all enriched turns in the session

Example:

```json
{
  "session": {
    "session_id": "s1",
    "trajectory_id": "t1",
    "parent_session_id": null,
    "vendor": "claude_code",
    "context": {
      "teammate": false,
      "multi_agent": false
    }
  },
  "turns": [
    {
      "turn_id": "turn-1",
      "user_request": "analyze the schema design",
      "steps": [
        {
          "step_id": "step-1",
          "event_refs": [
            {
              "event_id": "e1",
              "type": "user.prompt.submitted",
              "category": "user_interaction"
            },
            {
              "event_id": "e2",
              "type": "tool.call.requested",
              "category": "tool_call"
            }
          ]
        }
      ],
      "enrichment": {
        "notes": []
      }
    }
  ]
}
```

Rules:

- session stays canonical at the top
- session may include compact workflow operations with evidence refs
- turns are enriched for reading
- return all turns, not only selected turns

## Turn

### `turn overview <turn_id>`

Purpose:
Return one reconstructed turn in reading form.

Shape:

- user request
- steps
- tool actions
- event references

## Step

### `step overview <step_id>`

Purpose:
Return one step in reading form.

Shape:

- items
- event references
- compact feature operations with event/session refs when present

## Event

### `event get <event_id>`

Purpose:
Return the exact canonical event detail.

Shape:

- canonical event fields only

## Raw

### `raw <event_id>`

Purpose:
Return the exact raw log record for a canonical event.

Shape:

- source file path
- raw line number
- raw vendor payload

This is the verification escape hatch, not the main reading interface.

## Default Behavior

The default behavior should be:

- show reconstructed session structure
- show enriched understanding
- suppress execution-recording noise
- keep outputs small
- preserve stable ids for drill-down

## What The CLI Should Hide By Default

These should not dominate normal analysis output:

- hook progress logs
- file snapshot bookkeeping
- transport-level tool protocol chatter
- execution-recording messages that do not change session meaning
- repeated low-value vendor metadata

They may still exist in raw evidence, but they should not pollute the normal
overview APIs.

## Most Important Fields

The LLM mainly needs:

- stable ids
- session tree
- operations
- type
- category
- relationship context
- user request
- steps
- event refs
- source path and source line for raw evidence

## Recommendation

The intended reading flow is:

1. `trajectory overview <trajectory_id>`
2. `session overview <session_id>`
3. `turn overview <turn_id>`
4. `step overview <step_id>`
5. `event get <event_id>` when atomic event detail is needed
6. `raw <event_id>` when exact source proof is needed
