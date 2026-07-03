# Dashboard Agent and Service Lifecycle Architecture

## Purpose

The dashboard is adding agent-backed workflows beyond the original
context-window session analysis. Those workflows need a shared architecture so
each feature can add agent analysis, fix planning, approval, batching, or cache
support without leaking Codex app-server lifecycle details into React routes.

This document defines the target boundary between:

- React dashboard routes and UI primitives;
- the Python dashboard server;
- Codex app-server conversations;
- ordinary dashboard projection, job, and cache services.

## Architecture Rule

Dashboard features should use dashboard-owned handles, not external process
handles.

```
React route
  -> dashboard API
  -> dashboard-owned session/job/cache handle
  -> external process or core service
```

The frontend can own prompt assembly, display choices, and user interaction.
The dashboard server owns lifecycle, batching, cache, and process/session
management. The core service owns canonical facts and metric semantics.

## Boundaries

### Frontend

Frontend routes are feature owners. They should:

- assemble the task prompt or structured input from route-specific data;
- choose whether the response is plain text or schema-shaped JSON;
- parse feature-specific JSON when a route requests structured output;
- render feature-specific response UI;
- decide which user actions are allowed after an agent response.

Frontend routes should not:

- manage raw Codex app-server processes;
- assume Codex app-server thread ids are stable API handles;
- duplicate server-side cache or batching behavior;
- reconstruct core metric semantics that belong in `ct api` or dashboard
  server projection code.

### Dashboard Server

The Python dashboard server is the lifecycle owner. It should:

- expose stable dashboard-owned handles such as `job_id`,
  `agent_session_id`, and cache keys;
- keep page-lived agent sessions in memory;
- own Codex app-server process/thread lifecycle;
- batch and deduplicate expensive dashboard requests;
- apply TTL cleanup for in-memory state;
- keep API responses typed and narrow.

The server should remain thin in domain logic. It may orchestrate and shape
data, but it should not become a second metrics engine.

The server owns request recovery handles as well as conversation handles.
`job_id` recovers one in-flight or recently completed operation. An
`agent_session_id` recovers the page-lived agent conversation that may submit
many jobs over time. The two ids should be linked server-side so a refreshed
page can resume polling a known job and then continue the same agent session if
the server has not evicted it.

The server also owns one process-lifetime `CodexAppServerManager`. Dashboard
agent features should reuse that manager instead of spawning a fresh
`codex app-server` subprocess per analysis, per turn, or per page-lived agent
session.

### Codex App Server

Codex app-server owns the actual agent conversation execution. The dashboard
server may create an app-server thread and run turns, but React should not use
the raw app-server thread id as its primary continuation contract.

Dashboard-created app-server threads should be ephemeral by default. Durable
Codex thread persistence is an explicit future mode, not the default behavior
for dashboard analysis.

### Core Service

The core `ct api` service remains the source of truth for normalized sessions,
usage, tool evidence, and context facts. Dashboard projection code may batch
core calls and enrich with dashboard-specific presentation data such as pricing,
but should not redefine canonical semantics.

## Agent Session Design

Agent-backed UI should use an in-memory `AgentSessionStore`.

```
React route
  POST /api/agent-sessions
  <- agent_session_id

React route
  POST /api/agent-sessions/{agent_session_id}/turns
  -> prompt, optional output_schema
  <- job_id

React route
  GET /api/jobs/{job_id}
  <- agent turn result
```

The store maps dashboard session ids to the app-server resources needed to
continue the conversation:

```
agent_session_id -> {
  app_server_thread_id,
  cwd,
  created_at,
  last_used_at,
  route_scope,
  queued_turns,
  active_job_id,
  recent_job_ids,
}
```

### Lifecycle

Agent sessions are page-lived and server-memory-lived:

- A page may keep using an `agent_session_id` while the dashboard server is
  alive.
- A page refresh may continue the agent session only if the frontend preserves
  the `agent_session_id` in URL state or `sessionStorage`.
- A dashboard server restart loses all agent sessions.
- The server evicts inactive sessions by TTL.
- Dashboard shutdown must close app-server pipes, stop reader threads, and
  terminate the shared app-server process.
- Eviction and explicit close remove dashboard session metadata only; they do
  not own a separate subprocess.
- No local durable transcript files are required for generic agent tasks.

This is ephemeral relative to disk and server restarts, but not disposable after
each turn.

### Refresh and Recovery

The frontend may preserve both `agent_session_id` and the current `job_id` in
URL state or `sessionStorage`.

- `job_id` is the recovery handle for a single request. After a page refresh,
  the route can keep polling `/api/jobs/{job_id}` until the operation reaches a
  terminal state.
- `agent_session_id` is the recovery handle for conversation continuity. After
  the job is ready, the route can submit follow-up turns to the same session.
- The server should keep a small session-side index of active and recent job ids
  so it can answer whether a session has recoverable work.
- A missing, expired, or restarted-away `agent_session_id` should return a typed
  `not_found` or `expired` error, not a generic agent failure.

The recovery target is page refresh/reload while the same dashboard server is
alive. It is not recovery after server restart.

Frontend persistence such as `sessionStorage` may outlive the dashboard server,
so persisted `job_id` or `agent_session_id` values are recovery hints only. If a
restarted server returns `unknown job_id`, `agent_session_not_found`, or
`expired`, the route must clear the stored handle and return to an idle state
rather than displaying stale in-flight work.

### Turn Queue

Each `agent_session_id` owns a FIFO queue for turn execution. The dashboard
server should not run two turns concurrently against the same app-server
thread/process.

When a turn request arrives:

1. Validate the `agent_session_id` and enqueue the prompt request.
2. Return a `job_id` for that queued turn.
3. Run queued turns serially for that session.
4. Allow different agent sessions to run concurrently subject to the dashboard
   job runner's global worker limits.

This keeps the shared app-server conversation ordered while preserving
dashboard-level concurrency across unrelated sessions.

### Why Raw App-Server Thread IDs Are Not Enough

Raw app-server ids are implementation details. If the dashboard starts a fresh
`codex app-server --stdio` subprocess for every turn and terminates it after
the response, an ephemeral app-server thread cannot reliably survive to the
next browser request. The dashboard server must hold the live process/session
state if it wants short-lived multi-turn continuity.

The frontend should therefore pass `agent_session_id`, not
`app_server_thread_id`.

### Prompt and Response Ownership

The generic agent API should not define `task_goal`, `task_context`, or a
single response schema for every route. Each invoker owns those decisions.

The generic server contract should stay close to:

```json
{
  "agent_session_id": "dashboard-owned id",
  "prompt": "full route-assembled prompt",
  "output_schema": null
}
```

Plain text and structured output are both valid. If a route asks for structured
JSON, that route owns parsing, validation, and rendering of the parsed result.
The generic agent server should return transport-level data: ids, status,
diagnostics, and raw response text. Specialized route APIs may validate
route-specific schemas, but that validation should not move into the generic
agent-session endpoint.

## Job Design

Long-running dashboard operations should continue to use the existing async job
pattern:

```
POST /api/some-operation
<- 202 { "job_id": "..." }

GET /api/jobs/{job_id}
<- pending | running | ready | error
```

Agent turns should use this same job surface because model calls may take
minutes. The job result can include:

- `agent_session_id`;
- `app_server_turn_id` for diagnostics;
- raw `response_text`;
- transport and lifecycle diagnostics needed for debugging.

`job_id` is not a conversation handle. It is a handle for one async operation.
The server should retain enough job metadata to let a refreshed browser recover
an in-flight request by job id, but follow-up conversation turns must continue
through `agent_session_id`.

## Projection Cache Design

Ordinary dashboard projections should use a separate cache from agent sessions.

Candidate cache users:

- `/api/overview`;
- `/api/model-usage`;
- `/api/error-collection`;
- `/api/sessions/context-window`;
- cleanup previews;
- expensive batched `ct api` projections.

Cache entries should be deterministic by endpoint and normalized query params:

```
cache_key = endpoint + normalized_query + relevant_schema_version
```

The cache should support:

- short TTLs for fast dashboard navigation;
- explicit refresh bypass;
- concurrent request deduplication;
- schema/version invalidation;
- no caching for operations that mutate state.

Agent sessions and projection caches must remain separate:

- agent sessions are stateful conversations;
- projection cache entries are deterministic data snapshots.

## Current Implementation

The dashboard uses a server-owned app-server manager and in-memory agent
session store:

- `CodexAppServerManager` owns one app-server subprocess for the dashboard
  server lifetime.
- `AgentSessionStore` maps dashboard `agent_session_id` values to ephemeral
  app-server thread ids and route metadata.
- Session analysis uses the shared manager and runs from the recorded session
  project directory when available.
- Frontend routes use dashboard-owned `job_id` and `agent_session_id` handles,
  not raw app-server process handles.
- Restart recovery is intentionally unsupported for ephemeral dashboard agent
  sessions.

## API Impact

This architecture does not require changing every existing API, but it does
define which primitive each API family should use.

### Stateful Agent APIs

Generic agent APIs use dashboard-owned handles because they need multi-turn
continuity while the server process is alive:

- keep async `job_id` for turn execution;
- keep prompt and output-schema ownership with the invoker;
- keep generic agent sessions in memory only;
- default app-server threads to ephemeral.

The legacy `/api/agent-turn` path should remain a compatibility path only; new
route work should use `agent_session_id`.

### Async One-Shot APIs

Long-running one-shot APIs should use the shared job surface but do not need
agent sessions unless they need follow-up UX:

- context-window session analysis;
- session-analysis cached artifact generation;
- future batch cleanup previews or large import/export tasks.

These APIs can keep specialized request and result shapes. They should use the
shared app-server manager, default to ephemeral threads, and avoid exposing
app-server thread ids as continuation handles. If they use an agent internally,
that agent execution is an implementation detail unless the UI needs to
continue the conversation.

### Deterministic Read APIs

Read-only projections should use deterministic cache keys and refresh controls,
not agent sessions:

- deterministic dashboard projections;
- `/api/overview`;
- `/api/model-usage`;
- `/api/error-collection`;
- `/api/sessions/context-window`;
- cleanup previews.

These APIs may adopt the projection cache when they are expensive or repeatedly
queried during dashboard navigation. They should continue to source canonical
session and metric facts from core `ct api` contracts.

### Mutating APIs

Mutating operations should use explicit command endpoints and job ids when they
are long-running:

- cleanup apply APIs;
- future approval or fix-application workflows.

Mutations should not be cached. If an agent proposes a mutation, the route owns
the approval UX and submits a separate explicit command to the dashboard server.

## Implementation Plan

1. Maintain `CodexAppServerManager`.
   - One app-server subprocess per dashboard server process.
   - Serialized JSON-RPC access over one connection.
   - Shutdown closes pipes, reader threads, and the subprocess.
   - Dashboard agent work defaults to ephemeral app-server threads.

2. Maintain `AgentSessionStore`.
   - In-memory map keyed by `agent_session_id`.
   - Stores route metadata, cwd, app-server thread id, and recent job ids.
   - TTL eviction removes metadata for inactive sessions.
   - Restart clears the map and invalidates frontend recovery handles.

3. Keep agent session endpoints.
   - `POST /api/agent-sessions`.
   - `POST /api/agent-sessions/{id}/turns`.
   - `GET /api/agent-sessions/{id}` for existence/recovery state.
   - Optional endpoint to close a session explicitly.

4. Keep per-session turn queueing.
   - Serialize turns for each `agent_session_id`.
   - App-server manager serializes actual JSON-RPC turns on one connection.
   - Return a distinct `job_id` for every queued turn.

5. Maintain frontend agent hook.
   - Store `agent_session_id`, not app-server thread id.
   - Persist `agent_session_id` and active `job_id` in route state or
     `sessionStorage` for refresh recovery while the server stays alive.
   - Handle typed expired/not-found session responses by resetting the local
     agent state.

6. Keep overview agent analysis on the agent-session API.
   - Overview continues assembling its own prompt.
   - Overview renders plain text and owns follow-up UI.

6. Leave specialized session analysis unchanged initially.
   - Keep structured schema and cached artifacts for context-window analysis.
   - Revisit only when it needs generic follow-up or approval flow.

7. Add projection cache separately.
   - Start with one expensive read-only endpoint.
   - Include TTL, refresh bypass, and concurrent deduplication.

8. Classify the remaining dashboard APIs by primitive.
   - Agent sessions for stateful multi-turn agent UX.
   - Jobs for long-running one-shot operations.
   - Projection cache for deterministic read-only views.
   - Explicit command endpoints for mutations.

## Non-Goals

- Durable generic agent transcripts on disk.
- Recovery after dashboard server restart.
- A universal response schema for all agent tasks.
- Moving core metric semantics into the dashboard server.
- Replacing all existing dashboard APIs with agent-session APIs.

## Decision Summary

Use ephemeral, server-memory-owned agent sessions for generic dashboard agent
workflows. The browser owns UX and feature-specific prompt/rendering. The
dashboard server owns session handles, per-session turn queues, job recovery
maps, projection cache, and external process lifecycle. Codex app-server owns
agent execution. Core `ct api` remains the source of truth for session and
metric semantics.
