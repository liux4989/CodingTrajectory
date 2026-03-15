
# Background
There are many scattered coding agent logs, either are for 'runtime' execution recording puporse which is too noisy for post-analysis and runtime monintoring, or each coding agent's has different representation for the the same concepts  and has each angonistic feature which both of them need pre-processing the contextural connection based on each offcial docs.

# Goals
- Reconstruct the 'coding agent loop 
- Enrich Specific vendors' feature

# Architecture

# Core Layer Boundary
- `Event`, `Step`, `Turn`, and `Session` are canonical normalized resources.
- Canonical means agent-agnostic facts and stable references reconstructed from logs, not raw vendor JSONL and not UI-specific interpretation.
- `Trajectory` is a structural aggregate over canonical sessions. It may derive graph structure such as membership, edges, and summary metadata.
- Replay/UI-oriented interpretations such as sections, operations, roles, and workflow-specific labels should live in a projection or consumer layer, not the core hierarchy.

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
