# CLI Agent Notebook

This tutorial is written as an interactive notebook for agents. Each cell has a
goal, a command, and the useful signal to extract before moving to the next
cell.

Source session:

```text
019e8d04-1f38-79f3-b5a8-2e36a86be145
```

## Rule Of Thumb

Use YAML when reading structure. Use JSON when querying exact fields.

```bash
SESSION=019e8d04-1f38-79f3-b5a8-2e36a86be145
```

## Cell 1: Orient On The Last Turn

Read only the latest visible turn in YAML. This is the cheapest first pass when
an agent needs to understand what happened recently.

```bash
ct session overview "$SESSION" --turns 1 --format yaml
```

Useful signal from this source session:

```yaml
turn_id: 966ef7a6-670c-57bc-a6d4-a3e5299a0086
user_request:
  content: let us do it
step_ids:
- 9d38f678-a9c7-5205-a659-6043c5e83da5
- ...
- ff40b2bc-1c1a-5ee6-887f-6b49f4d5655e
```

Decision: the last turn is the implementation turn. Use its step ids for detail
drill-down instead of reading the whole raw log.

## Cell 2: Find The Expensive Turn

Use JSON when selecting a turn by exact metrics.

```bash
ct session usage "$SESSION" --format json \
  | jq '.turns | max_by(.cost_usd) | {
      seq,
      turn_id,
      activity,
      cost_usd,
      input: .tokens.input_tokens,
      output: .tokens.output_tokens,
      cache_reuse: .efficiency.cache_reuse_ratio
    }'
```

Expected signal from this source session:

```json
{
  "seq": 7,
  "turn_id": "966ef7a6-670c-57bc-a6d4-a3e5299a0086",
  "activity": "response-heavy activity",
  "cost_usd": 1.908735,
  "input": 1981593,
  "output": 12623,
  "cache_reuse": 0.9395
}
```

Decision: turn `966ef7a6-670c-57bc-a6d4-a3e5299a0086` dominates cost, so inspect
that turn before spending tokens on earlier turns.

## Cell 3: Read Turn Usage In YAML

Use YAML when an agent needs to read the usage payload without writing a query.

```bash
ct session usage "$SESSION" \
  --turn 966ef7a6-670c-57bc-a6d4-a3e5299a0086 \
  --format yaml
```

Useful fields:

```yaml
turn_id: 966ef7a6-670c-57bc-a6d4-a3e5299a0086
seq: 7
tokens:
  input_tokens: 1981593
  cached_input_tokens: 1861760
  output_tokens: 12623
efficiency:
  cache_reuse_ratio: 0.9395
cost_usd: 1.908735
cost_drivers:
- kind: tool_steps
  cost_usd: 1.254512
- kind: response_steps
  cost_usd: 0.654223
```

Decision: the turn is expensive mostly because of tool steps, but the command
still avoids expanding paths, commands, and raw tool output. Use step detail for
evidence.

## Cell 4: Drill Into A Specific Step

Use the first implementation step from the overview.

```bash
ct session step-detail 9d38f678-a9c7-5205-a659-6043c5e83da5 --format yaml
```

Expected signal:

```yaml
- step_id: 9d38f678-a9c7-5205-a659-6043c5e83da5
  type: assistant_response
  operations:
  - text_reply
  shape:
    texts:
    - 'I’ll implement this as a scoped CLI/service refactor: `stats` gets the context-style
      usage overview, `usage` becomes the turn-level cost/efficiency surface...'
  event_ids:
  - 4e3371f6-69c4-5c36-bb13-5aaaa0f41b87
```

Decision: this step is an assistant planning response. Continue through the
step ids when looking for edits, commands, or verification evidence.

## Cell 5: Save A Stable JSON Artifact

Use `--output` for artifacts. It always writes JSON, even if stdout would be
YAML or text.

```bash
ct session overview "$SESSION" --turns 1 --format yaml --output /tmp/ct-overview.json
jq '.sessions[0].turns[0].step_ids | length' /tmp/ct-overview.json
```

Expected result for this source session:

```text
53
```

Decision: use stdout format for immediate reading, and `--output` for stable
automation.

## Recommended Agent Flow

1. Start with `overview --format yaml --turns N`.
2. Use `usage --format json | jq ...` to select costly or suspicious turns.
3. Use `step-detail --format yaml` for readable evidence.
4. Use `event-detail --format json` only when exact raw content or a scriptable
   field is needed.
