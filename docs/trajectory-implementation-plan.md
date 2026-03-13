# Trajectory Graph Implementation Plan

This plan turns the richer `trajectory.get` contract in [`docs/session-api.json`](./session-api.json) into executable changes across ingestion, assembly, query, and CLI serialization.

## Goals

- Preserve the current replay-first resource model:
  - `session.get` remains the detailed replay entrypoint
  - `turn.get` and `event.get` remain reference-resolution endpoints
- Add a trajectory assembly layer that can represent both:
  - cross-session multi-agent orchestration
  - in-session multi-agent orchestration
- Keep adapters focused on vendor-local facts, not UI sections or weak global inference

## Non-Goals

- Do not embed full events inside `trajectory.get`
- Do not force all vendors into the same multi-agent topology
- Do not move replay responsibility away from `session.timeline`

## Topology Model

The implementation should recognize two valid multi-agent shapes.

### 1. Cross-Session Multi-Agent

Primary example: Claude Code sidechains.

Characteristics:

- one or more related sessions
- parent-child or peer session relationships
- subagent / teammate work represented by separate session ids
- trajectory relations are expressed primarily by:
  - `session_refs`
  - `edges`
  - `operations` with `scope = "session_graph"`

### 2. In-Session Multi-Agent

Primary example: Codex orchestration inside one session.

Characteristics:

- one session containing the orchestration span
- delegation / collaboration encoded as events or tool activity
- trajectory relations are expressed primarily by:
  - `operations` with `scope = "session_span"`
  - `sections`
  - event evidence rather than new session ids

### 3. Hybrid

Some future trajectories may include both patterns.

The trajectory should expose:

- `multi_agent_mode = "cross_session"`
- `multi_agent_mode = "in_session"`
- `multi_agent_mode = "hybrid"`

## File-Level Plan

## 1. `src/coding_trajectory/ingestion/models.py`

Add new trajectory-level data models.

New models:

- `TrajectorySummary`
- `TrajectorySessionRef`
- `TrajectoryEdge`
- `TrajectoryOperation`
- `TrajectorySection`
- `InferenceNote`

Extend `Trajectory` with optional fields:

- `multi_agent_mode`
- `summary`
- `session_refs`
- `edges`
- `operations`
- `sections`
- `inference_notes`

Rules:

- Keep `sessions` for canonical resource storage and compatibility
- Default all new list fields to empty lists
- Keep all new trajectory fields optional at the model layer during rollout
- Do not add presentation-only fields to `Session`, `Turn`, or `Event`

## 2. `src/coding_trajectory/ingestion/adapters/claude_code.py`

Purpose: improve local evidence quality for cross-session assembly.

Changes:

- Preserve every available linkage field in event payloads and extensions:
  - `parent_uuid`
  - `logical_parent_uuid`
  - `request_id`
  - `team_name`
  - `agent_id`
  - `agent_name`
  - `is_sidechain`
- Audit whether Claude logs expose explicit subtask completion or teammate completion markers
- If the raw logs clearly imply completion, derive normalized completion events conservatively
- Preserve compaction metadata with enough fidelity for operation assembly:
  - trigger
  - token counts
  - summarized message counts

Do not:

- emit `sections`
- emit trajectory `edges`
- guess parent sessions from weak heuristics inside the adapter

## 3. `src/coding_trajectory/ingestion/adapters/codex.py`

Purpose: improve local evidence quality for in-session assembly.

Changes:

- Audit real Codex logs for orchestration markers such as:
  - subagent spawn
  - handoff
  - teammate work
  - resume-like transitions
- Preserve stable grouping fields in payloads wherever available
- Keep collaboration metadata in extensions:
  - `collaboration_mode`
  - `agent_role`
  - `agent_nickname`
- If an operation span is visible only through event patterns, keep the raw evidence in event payloads so the assembler can group it later

Do not:

- fabricate extra sessions
- force Codex orchestration into `parent_session_id`

## 4. `src/coding_trajectory/ingestion/decorators.py`

Purpose: handle cross-session enrichment that belongs above adapters and below trajectory assembly.

Changes:

- Keep `ClaudeCodeDecorator` as the only vendor decorator for now
- Strengthen parent resolution for Claude sidechains using:
  - `parent_uuid` when usable
  - `team_name`
  - `is_sidechain`
  - session/vendor filtering
- Prefer deterministic linkage over loose first-match logic
- If no strong parent can be resolved, leave `parent_session_id` unset and let trajectory inference notes explain uncertainty

Do not:

- add Codex/Gemini/Amp decorators yet
- build trajectory sections here

## 5. `src/coding_trajectory/discovery.py`

This file needs the largest structural change.

Current issue:

- trajectories are grouped by normalized project key only
- separate tasks and resumptions inside one repo are collapsed into one trajectory

Changes:

- Split discovery into two stages:
  - session ingestion and stabilization
  - trajectory assembly
- Introduce an assembly function, for example:
  - `assemble_trajectories(sessions: list[Session]) -> list[Trajectory]`
- Apply decorators before trajectory assembly
- Replace project-only grouping with a work-thread grouping heuristic

Initial grouping strategy:

- first group by project / workspace identity
- then split by stronger task/thread evidence where available:
  - Claude:
    - parent-child sidechain linkage
    - request / parent uuid chains
    - team_name clusters
  - Codex:
    - session id as the default work unit for in-session orchestration
    - merge only when later evidence proves continuity

Assembler outputs per trajectory:

- `multi_agent_mode`
- `summary`
- `session_refs`
- `edges`
- `operations`
- `sections`
- `inference_notes`

## 6. New assembly module

Add a dedicated module, for example:

- `src/coding_trajectory/trajectory.py`

Responsibilities:

- classify trajectory topology:
  - cross-session
  - in-session
  - hybrid
- derive session refs from sessions and extensions
- derive edges from:
  - `parent_session_id`
  - sidechain metadata
  - resume evidence
  - explicit handoff evidence when present
- derive operations from grouped event evidence
- derive sections as ordered presentation slices
- generate inference notes whenever a relationship is not directly observed

Recommended internal functions:

- `build_trajectory_summary(...)`
- `build_session_refs(...)`
- `build_edges(...)`
- `build_operations(...)`
- `build_sections(...)`
- `build_inference_notes(...)`
- `detect_multi_agent_mode(...)`

## 7. `src/coding_trajectory/query.py`

Purpose: keep query loading compatible with the expanded trajectory model.

Changes:

- continue to index `sessions`, `turns`, and `events` from `Trajectory.sessions`
- accept richer `Trajectory` payloads transparently via the expanded model
- avoid deriving trajectory graph structure here

Query should remain a loader/index, not an assembler.

## 8. `src/coding_trajectory/cli.py`

Purpose: serialize and summarize the richer trajectory shape.

Changes:

- update `serialize_trajectory_detail` to emit:
  - `multi_agent_mode`
  - `summary`
  - `session_refs`
  - `edges`
  - `operations`
  - `sections`
  - `inference_notes`
- keep `session_ids` for compatibility and field selection
- update `summarize_trajectory` to show higher-level signals:
  - session count
  - operation count
  - section count
  - multi-agent mode
- do not duplicate event bodies into trajectory summary output

## 9. Tests

### `tests/test_models.py`

Add coverage for:

- new trajectory-level model defaults
- compatibility with existing `Trajectory(project_identifier=..., task_reference=...)`

### `tests/test_claude_adapter.py`

Add coverage for:

- sidechain linkage evidence preserved in payloads/extensions
- compaction evidence shape
- conservative completion derivation if implemented

### `tests/test_codex_adapter.py`

Add coverage for:

- collaboration metadata retained
- operation-grouping evidence preserved in payloads

### `tests/test_cli.py`

Add coverage for:

- `trajectory get` raw output includes new fields
- `trajectory get --fields ...` works with new trajectory properties
- `trajectory list --view summary` remains concise

### New assembly tests

Add a focused test file, for example:

- `tests/test_trajectory_assembly.py`

Cover:

- Claude cross-session trajectory assembly
- Codex in-session trajectory assembly
- hybrid trajectory assembly
- inference note generation when links are partial

## Rollout Order

### Phase 1: Data Model

- add trajectory-level models
- keep serializer compatibility

### Phase 2: Adapter Evidence Quality

- improve Claude linkage evidence
- improve Codex orchestration evidence

### Phase 3: Assembly Layer

- add dedicated trajectory assembler
- apply decorators before assembly
- replace project-only grouping

### Phase 4: Serialization

- update CLI serialization and summaries
- keep `session.get`, `turn.get`, `event.get` unchanged

### Phase 5: Tests and Fixtures

- add assembly-focused tests
- expand adapter and CLI coverage

## Acceptance Criteria

- A Claude sidechain session can appear as a cross-session subagent operation without embedding full events in `trajectory.get`
- A Codex collaboration-heavy session can appear as in-session operations and sections without fabricating child sessions
- `trajectory.get` can express both topologies with one schema
- Existing session/turn/event replay remains intact
- Project grouping no longer collapses unrelated work into a single trajectory by default

## Practical Mapping for Current Known Examples

Claude session `c4ef6899-8915-4654-98ec-cd96fdc98969`

- vendor: `claude_code`
- likely topology: `cross_session`
- likely needs:
  - resolved parent session
  - `sidechain_of` or `spawned_subagent` edge
  - `subagent` operation with `scope = "session_graph"`

Codex session `019ce6f9-8f76-7692-ac04-34506fb3dcf0`

- vendor: `codex_cli`
- likely topology: `in_session`
- likely needs:
  - session-local operation grouping
  - `subagent` or `handoff` operations with `scope = "session_span"`
  - sections derived from event spans, not extra sessions
