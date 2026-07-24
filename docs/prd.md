
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

# Ingestion Transcript Layer
- Each vendor adapter keeps vendor-specific parsing local, then emits a small transcript record stream: user message, assistant message, tool call, tool result, usage/runtime, and task completion.
- Adapters deserialize only fields that contribute to hierarchy, transcript, tool reconstruction, usage, status, or session linkage.
- Transcript records carry CT-owned normalized `data`; lossy or synthetic records are explicitly marked with transcript fidelity.
- A shared transcript projector owns the `Session -> Turn -> Item` reconstruction rules, including turn starts, tool-call/result pairing, and final-answer fallback behavior.
- Provider-specific payloads remain in transcript `data` and canonical `vendor_data` only when they are useful to CT; unused raw log properties are skipped instead of modeled.

# Evaluation Projection Layer
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
