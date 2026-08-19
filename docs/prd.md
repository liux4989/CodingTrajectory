
# Background
There are many scattered coding agent logs, either are for 'runtime' execution recording puporse which is too noisy for post-analysis and runtime monintoring, or each coding agent's has different representation for the the same concepts  and has each angonistic feature which both of them need pre-processing the contextural connection based on each offcial docs.

# Goals
- Reconstruct the 'coding agent loop 
- Enrich Specific vendors' feature

# Architecture

# Core Layer Boundary
- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources.
- Canonical means agent-agnostic facts and stable references reconstructed from logs, not raw vendor JSONL and not UI-specific interpretation.
- `SessionGraph` is the orchestration aggregate over canonical sessions. Its identity is the root session id, and it exposes observed membership, orchestration capabilities, edges, and summary metadata.
- Replay/UI-oriented interpretations such as sections, operations, roles, and workflow-specific labels should live in a projection or enrichment layer, not the core hierarchy.
- Metric and context-window projections must be session-scoped first and graph-scoped second. A graph aggregate may expose totals, but it must not present child-agent turns, starting context, or provider context windows as if they belonged to the root session. Multi-session responses must expose explicit per-session sections alongside any graph totals.

# Living Events Protocol

## Resource structure
- The protocol is a revisioned change feed over canonical resources, not a flat replay of vendor events and not an assertion that a session is only a linear list of newly appended events.
- The canonical ownership hierarchy is `Session -> Turn -> Item`. Every turn has one owning session and every item has one owning turn, with stable IDs at each level.
- A protocol response preserves that ownership hierarchy even when pagination returns only a slice. A returned turn always carries its `session_id`, and a returned item always carries both its `session_id` and `turn_id`.
- `SessionGraph` is an orchestration overlay on the hierarchy. Its nodes reference canonical sessions and its evidence-backed edges represent relationships such as spawn, delegation, handoff, and resume. Agent sessions must not be represented only as nested children because orchestration relationships may form forks, joins, or other non-tree shapes.
- Each session owns its context checkpoints. A context checkpoint represents an observed agent compaction or equivalent context-boundary event and may divide the session into context epochs without changing the direct `Session -> Turn -> Item` ownership hierarchy.
- Turns should reference the effective `context_epoch_id` or preceding `context_checkpoint_id` when that relationship can be reconstructed. A checkpoint may also carry `effective_after_turn_id`, `effective_before_turn_id`, source event references, timestamps, and available token measurements.
- A context checkpoint is distinct from a source checkpoint. `context_checkpoints` describe agent context/compaction history; `source_checkpoint` describes durable ingestion progress such as source generation, committed byte offset, and source fingerprint.

The logical protocol shape is:

```text
SessionGraph
|- session nodes ------------------------------------+
`- orchestration edges                               |
                                                      v
Session
|- source_checkpoint
|- context_checkpoints[]
`- turns[]
   `- items[]
```

## Revision and change semantics
- The protocol cursor orders observations committed by CT; it does not replace domain identity or hierarchy. An increasing revision means that CT observed and published a change, not necessarily that a new turn was appended.
- Incremental responses use resource operations such as `upsert`, `remove`, and `reset`. Each operation carries a hierarchical resource address containing the applicable `session_id`, `turn_id`, `item_id`, `context_checkpoint_id`, or graph edge ID.
- Existing resources may be updated when a tool result completes a prior tool call, a child-session relationship is discovered, a checkpoint is reconstructed, or a source is replaced. Consumers therefore apply changes by stable resource identity rather than assuming append-only array positions.
- Pagination uses a stable `through` revision and keyset cursor. The same `through` value bounds hierarchy, graph edges, checkpoints, turns, and items so a consumer does not combine resources from inconsistent observations.
- Snapshot and incremental forms describe the same resources. A snapshot returns a bounded hierarchy/graph slice; an incremental response returns operations that can be applied to that same schema.

## View and details modes
- `view` and `details` share the same resource schema and stable IDs. The mode controls content representation, not canonical identity.
- `view` is a turn-oriented agent view. It retains complete user requests and agent response messages while replacing massive tool parameters, tool output, and patches with structured content references. It may retain useful normalized operation summaries, targets, status, counts, and item IDs, but it must not return human-only preview strings as the protocol contract.
- `details` expands those structured references with the complete canonical item content already exposed by CT item-detail projection. It remains free of redundant vendor rollout wrappers and lifecycle noise; raw JSONL is still the immutable audit authority.
- Truncation is never silent. Potentially large fields use a stable content envelope that indicates whether the value is inline or referenced and records its size and drill-down reference. The same envelope is used in both modes.
- Clients may scope either mode to a graph, session, turn, item, or context checkpoint. Deep ID queries avoid forcing a consumer to retrieve the full hierarchy when it needs one operation.

An illustrative response shape is:

```json
{
  "mode": "view",
  "through": "revision:1842",
  "graph": {
    "root_session_id": "session:root",
    "nodes": [{"session_id": "session:root"}],
    "edges": []
  },
  "sessions": [
    {
      "session_id": "session:root",
      "source_checkpoint": {},
      "context_checkpoints": [],
      "turns": [
        {
          "turn_id": "turn:42",
          "context_epoch_id": "epoch:2",
          "user_request": {},
          "items": [],
          "assistant_responses": []
        }
      ]
    }
  ],
  "next_cursor": null
}
```

# Ingestion Transcript Layer
- Each vendor adapter keeps vendor-specific parsing local, then emits a small transcript record stream: user message, assistant message, tool call, tool result, usage/runtime, and task completion.
- Adapters deserialize only fields that contribute to hierarchy, transcript, tool reconstruction, usage, status, or session linkage.
- Transcript records carry CT-owned normalized `data`; lossy or synthetic records are explicitly marked with transcript fidelity.
- A shared transcript projector owns the `Session -> Turn -> Item` reconstruction rules, including turn starts, tool-call/result pairing, and final-answer fallback behavior.
- Provider-specific payloads remain in transcript `data` and canonical `vendor_data` only when they are useful to CT; unused raw log properties are skipped instead of modeled.
- Core ingestion may apply a consumer-neutral retention policy after canonical identifiers are stabilized. `trajectory` retains replay evidence; `measurements` retains hierarchy, usage, runtime, pricing, tool outcomes, and reconciliation inputs while releasing transcript bodies. Both policies preserve the same canonical facts for their shared metric contracts.
- Retention policy must not introduce consumer concepts such as waste scores, rankings, dashboard cards, default horizons, or UI labels. Those remain projection-layer decisions, and immutable vendor logs remain the evidence authority for lazy detail reconstruction.
- Consumer-owned derived stores are replaceable artifacts, not canonical compatibility boundaries. An incompatible SQLite format must be rebuilt from immutable logs; core vendor compatibility and versioned public API contracts remain separate responsibilities.

# Evaluation Projection Layer
- Evaluation terminology and grader selection follow the adaptation of Anthropic's [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) recorded in the session-evaluation high-level design; CodingTrajectory's versioned contracts remain authoritative.
- Raw vendor logs are reconstruction and audit inputs, not default evaluator context.
- Evaluation starts from canonical `session.overview` and `session.items` projections and builds a versioned task contract plus bounded evidence records with stable evidence IDs.
- Rubric compilation receives requests, material requirement changes, compact turn structure, repository instructions, and validation authority without receiving the full outcome trajectory.
- Semantic judgment receives the final response and criterion-relevant observable evidence. It may request one bounded expansion by canonical evidence kind and turn ID; CT resolves the request and never grants unrestricted raw-log or checkout access.
- Executable verification and final aggregation remain deterministic CT-owned operations outside the evaluator agent.
- Evaluation artifacts record evidence selection, expansion, evaluator versions, and source fingerprints so reduced context does not weaken provenance.

# Infrastructural layer
- Discover : discocer all agent logs

- Reconstruct Tree
  - Trajector
  - Session : {session-id}
  - Turn : {user-request}  
  - Steps :
    -- LLMResponse: {response text}
    -- Tool : {tool_name} {category}{type}{params} {output}  #The raw event logs  will have lots of chain toolevents for a single tool, we can category and reduce to one tool event
    

# Desing Consideration
- there are many lifecycle events: like SESSION_STARTED,LLM_REQUEST_STARTED, e.t.c They are runtime execution logs. For our reconstruct tree, we don't need to show it. A hirerachy tree will show the current agent task status.
    - Current implementation problem: different agnet has different lifecycle names and insufficient lifecycle support, we are trying to support it all which leads to a massive events library and 'confidence' mechanism
- the event should be category first instead just show a chain events. 
- remove nosiy events: the raw logs also has many message for execution recoridng purpose.
- progressive disclousre: our api desing will be hireachy , we don't show all information via one method. and the details we prefer a deep id query.
- do not store heuristic or presentation-only interpretation in canonical fields.
