# PRD: Unified Coding Agent Trajectory & Metrics Platform

## 1. Overview

A platform to collect, normalize, and expose execution trajectories and metrics from heterogeneous coding agents (e.g., Codex CLI, Claude Code). The system ingests telemetry, lifecycle hooks, transcripts, and logs, producing a canonical trajectory graph and unified metrics model for replay, debugging, analytics, and evaluation.

## 2. Goals

* Provide a unified trajectory model for multiple coding agents
* Normalize metrics across agents for comparison and dashboards
* Enable replay and inspection of agent behavior
* Preserve raw vendor logs for auditability

## 3. Non-Goals

* Replace agent runtimes
* Modify agent execution logic
* Provide training pipelines (future work)

## 4. Target Users

* AI agent developers
* platform engineers
* research/evaluation teams
* dev productivity teams

## 5. Core Concepts

### 5.1 Trajectory

Logical work history for a task. May span multiple sessions.

### 5.2 Session

One concrete runtime execution (interactive run, CLI execution, or subagent instance).

### 5.3 Turn

A single user request and the agent work that follows. A turn spans many events within a session.

### 5.4 Event

Append-only timeline record capturing lifecycle actions.

### 5.5 Artifact

External outputs such as diffs, terminal output, tool results.

### 5.6 Metrics

Runtime and outcome measurements derived from telemetry or events.

## 6. Normalized Event Taxonomy

The platform exposes a common event taxonomy as a normalized output schema.
Providers do not necessarily emit the same native primitives or the same event
coverage in their raw logs. Adapters may therefore:

* map directly observed provider events into the normalized taxonomy
* derive normalized events from related log records, traces, or status fields
* leave unsupported events absent rather than fabricate false precision

The taxonomy below defines the common comparison surface for downstream replay,
analytics, and metrics. It is not a guarantee that every provider emits every
event natively.

### Session Lifecycle

* session.started
* session.resumed
* session.ended

### User Interaction

* user.prompt.submitted

### Model Execution

* llm.request.started
* llm.request.completed
* llm.stream.event

### Tool Lifecycle

* tool.call.requested
* tool.call.started
* tool.call.succeeded
* tool.call.failed

### Permission Flow

* permission.requested
* permission.approved
* permission.denied

### Subtasks

* subtask.started
* subtask.completed

### Background Tasks

* background_task.started
* background_task.completed

### Context Management

* context.compaction.started
* context.compaction.completed

### Completion

* agent.response.completed
* task.completed

## 7. Metrics Model

### Core Metrics

Session

* session.count
* session.duration_ms

Turn

* turn.count
* turn.duration_ms

LLM

* llm.request.count
* llm.request.duration_ms
* llm.token.input
* llm.token.output
* llm.cost.estimated

Tools

* tool.call.count
* tool.call.duration_ms
* tool.call.error_count

Outcome

* code.lines.accepted
* suggestion.accept_rate
* pr.count.assisted

## 8. Architecture

Ingestion Layer

* Agent adapters (Codex, Claude Code)
* OpenTelemetry collector
* Hook ingestion

Normalization Layer

* Event mapping
* Metric mapping
* Session reconstruction
* Turn reconstruction
* Event derivation from raw logs, traces, and lifecycle metadata

Provider coverage note

* Common events are normalized outputs, not shared provider primitives
* Coverage varies by vendor and runtime
* Consumers should treat absent events as "not available from this provider/log"
  unless a downstream contract defines stronger guarantees

Storage Layer

* Trajectory graph store
* Metrics store
* Raw log archive

API Layer

* session API
* trajectory API
* metrics API

UI

* timeline replay
* tool inspection
* cost dashboards

## 9. Data Model

Trajectory

* trajectory_id
* task_reference

Session

* session_id
* trajectory_id
* parent_session_id
* vendor
* started_at
* ended_at

Turn

* turn_id
* session_id
* user_request
* started_at
* ended_at

Event

* event_id
* session_id
* turn_id
* timestamp
* type
* actor
* provenance
* confidence
* payload
* vendor_source

Artifact

* artifact_id
* event_id
* type
* location

## 10. Key Features

### Trajectory Replay

Visual timeline reconstruction of agent actions.

### Cross-Agent Comparison

Compare metrics and behavior across agent implementations.

### Metrics Dashboard

Token usage, latency, cost, tool usage.

### Vendor Extensions

Allow agent-specific payloads without breaking schema.

### Event Provenance

Normalized events may be:

* observed directly from provider logs
* derived from related records or trace spans
* synthesized conservatively when required to reconstruct session or turn structure

Confidence expresses how strongly the normalized event semantics are supported by
the source data:

* high: direct mapping from explicit provider data
* medium: derived from stable adjacent records, trace names, or runtime metadata
* low: derived from brittle heuristics such as message text matching

## 11. Success Metrics

* ingestion success rate
* event normalization coverage
* supported agent count
* replay latency

## 12. MVP Scope

Agents

* Codex CLI
* Claude Code

Capabilities

* trajectory ingestion
* event normalization
* metrics normalization
* timeline UI

## 13. Future Extensions

* agent evaluation benchmarks
* RL trajectory datasets
* dataset export pipelines
* agent debugging automation
