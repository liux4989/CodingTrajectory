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

### Codex App Server

Codex app-server owns the actual agent conversation execution. The dashboard
server may create an app-server thread and run turns, but React should not use
the raw app-server thread id as its primary continuation contract.

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
  process/client reference or resumable app-server connection,
  created_at,
  last_used_at,
  route_scope,
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
- No local durable transcript files are required for generic agent tasks.

This is ephemeral relative to disk and server restarts, but not disposable after
each turn.

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
JSON, that route owns validation and rendering of the parsed result.

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
- optional parsed structured output when the invoker requested it and the
  server validates it.

`job_id` is not a conversation handle. It is a handle for one async operation.

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

## Current Implementation Gaps

The current generic agent flow is moving toward the right frontend ownership
model, but it still needs a server lifecycle refactor:

- It exposes raw app-server thread ids to the frontend.
- It starts a Codex app-server subprocess per turn.
- It terminates that subprocess after the turn.
- It has no dashboard-owned `agent_session_id`.
- It has no server-side in-memory agent session registry.

That means multi-turn continuity depends on behavior outside the intended
dashboard-server ownership boundary.

## API Impact

This architecture does not require changing every existing API.

### Should Change

Generic agent APIs should change first:

- replace raw `/api/agent-turn` continuation with dashboard-owned
  `agent_session_id`;
- keep async `job_id` for turn execution;
- keep prompt and output-schema ownership with the invoker;
- keep generic agent sessions in memory only.

Current generic invokers such as overview warning analysis should migrate to
this API.

### Can Stay

Specialized one-shot APIs can stay unless they need follow-up UX:

- context-window session analysis;
- session-analysis cached artifact generation;
- deterministic dashboard projections;
- cleanup preview/apply APIs.

Those APIs may later adopt the shared job/cache primitives, but they do not
need to become agent sessions by default.

## Implementation Plan

1. Add `AgentSessionStore`.
   - In-memory map keyed by `agent_session_id`.
   - TTL eviction.
   - Owns app-server thread/process lifecycle.

2. Add agent session endpoints.
   - `POST /api/agent-sessions`.
   - `POST /api/agent-sessions/{id}/turns`.
   - Optional endpoint to close a session explicitly.

3. Update frontend agent hook.
   - Store `agent_session_id`, not app-server thread id.
   - Optionally persist it in route state or `sessionStorage` for refresh
     recovery while the server stays alive.

4. Migrate overview agent analysis.
   - Overview continues assembling its own prompt.
   - Overview renders plain text and owns follow-up UI.

5. Leave specialized session analysis unchanged.
   - Keep structured schema and cached artifacts for context-window analysis.
   - Revisit only when it needs generic follow-up or approval flow.

6. Add projection cache separately.
   - Start with one expensive read-only endpoint.
   - Include TTL, refresh bypass, and concurrent deduplication.

## Non-Goals

- Durable generic agent transcripts on disk.
- Recovery after dashboard server restart.
- A universal response schema for all agent tasks.
- Moving core metric semantics into the dashboard server.
- Replacing all existing dashboard APIs with agent-session APIs.

## Decision Summary

Use ephemeral, server-memory-owned agent sessions for generic dashboard agent
workflows. The browser owns UX and feature-specific prompt/rendering. The
dashboard server owns session handles, jobs, cache, and external process
lifecycle. Codex app-server owns agent execution. Core `ct api` remains the
source of truth for session and metric semantics.
