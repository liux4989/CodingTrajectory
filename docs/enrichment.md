# Enrichment Layer

## Purpose

The enrichment layer adds useful interpretation on top of core data without
changing the canonical source of truth.

- Core remains the source of truth.
- Enrichment is optional.
- Clients may read core resources directly, or combine them with enrichment for
  better structural understanding.

## Boundary

Core resources:

- `Event`
- `Step`
- `Turn`
- `Session`
- `Trajectory` as a structural aggregate

Enrichment resources:

- structural views such as grouped operations and sections
- derived understanding state such as labels, statuses, and notes
- agent-specific overlays for special workflows

Enrichment must never overwrite canonical fields.

## Wrapper Shape

Enriched resources refer to canonical data by ID instead of embedding the full
canonical payload.

Example:

```json
{
  "trajectory_id": "11111111-1111-1111-1111-111111111111",
  "enrichment": {
    "structural": {
      "operations": []
    },
    "derived": {
      "multi_agent_mode": "cross_session"
    },
    "agent_specific": {
      "codex": {
        "collaboration_mode": "default"
      }
    },
    "plugins": {
      "codex.spawn": {
        "derived": {
          "multi_agent_mode": "cross_session"
        }
      }
    },
    "notes": []
  }
}
```

This keeps the boundary explicit:

- core data is fetched from canonical APIs
- enrichment points to the canonical resource by ID
- derived state lives only in the sidecar

## Plugin Model

Plugins read canonical resources and return enrichment overlays.

Rules:

- plugins must not mutate canonical resources
- plugin output is attached under its namespace in `enrichment.plugins`
- any inferred value should carry provenance and confidence

## Recommended API Shape

Canonical APIs:

- `trajectory.get`
- `session.get`
- `turn.get`
- `step.get`
- `event.get`

Enrichment APIs:

- `trajectory.enrich`
- `session.enrich`

Optional convenience bundle:

- `trajectory.bundle`
- `session.bundle`

Bundle responses may include both canonical and enrichment payloads, but they
should still remain separated as sibling objects.
