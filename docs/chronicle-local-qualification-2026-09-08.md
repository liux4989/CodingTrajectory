# Chronicle Local Qualification — 2026-09-08

- **Source revision:** `d73a9e28` plus the two corrections described below
- **Result:** Passed locally; no remote state changed
- **Evidence boundary:** Aggregate counts only; source paths, project names,
  session identifiers, and payload bodies are omitted

## Scope

The qualification built `ct.chronicle_graph.v1` artifacts from available local
vendor sources. Codex and Claude Code used the seven-day CodingTrajectory
project scope. Pi had no source in that project window, so it used a seven-day
global fallback. The project-scoped Amp source had no operational items, so Amp
used a thirty-day global fallback.

Each graph passed:

- strict body-free Pydantic validation;
- the 8 MiB artifact and 16 MiB per-project publication bounds;
- byte-identical build, round-trip, and rebuild;
- session, turn, item, and edge identity parity;
- all 13 body-free Chronicle API executions; and
- numeric parity for stats, usage, model usage, request usage, and tool usage
  against the originating full local graph.

## Aggregate result

| Vendor | Scope | Sources | Graphs | Sessions | Turns | Items | Max artifact bytes | API calls | Numeric values checked |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Codex CLI | Project, 7 days | 32 | 20 | 30 | 196 | 7,276 | 2,218,404 | 260 | 71,294 |
| Claude Code | Project, 7 days | 2 | 2 | 2 | 10 | 417 | 276,455 | 26 | 3,978 |
| Pi | Global, 7 days | 17 | 17 | 17 | 57 | 1,675 | 426,936 | 221 | 30,321 |
| Amp | Global, 30 days | 2 | 2 | 2 | 3 | 23 | 14,832 | 26 | 293 |
| **Total** | — | **53** | **41** | **51** | **266** | **9,391** | **2,218,404** | **533** | **105,886** |

The artifacts retained 4,181 sanitized operational details and 2,671 explicit
projection links. Their aggregate encoded size was 9,026,978 bytes across
multiple projects; the gate evaluated the 16 MiB publication limit separately
for each project.

## Defects found and corrected

The live sources exposed two failures not represented by the committed metric
fixtures:

1. Agent-collaboration usage entered the coordination bucket but the rendered
   category list omitted that bucket. One projection-heavy Codex graph therefore
   failed the allocated-usage reconciliation assertion. The category is now
   rendered as `Agent collaboration`.
2. Chronicle round-trip restored projection provenance, but a subsequent
   artifact build read only vendor-native activity provenance. Fifteen of twenty
   Codex graphs consequently lost projection parent links on rebuild. Artifact
   construction now falls back to restored Chronicle provenance.

After both corrections, all selected graphs passed deterministic replay and the
full local gate.

## Reproduction

Run from a checkout containing the source revision and corrections:

```sh
PYTHONPATH=packages/core/src:packages/cli/src \
  uv run --package coding-trajectory python \
  scripts/validate-chronicle-local.py --project-root <recorded-project-root>
```

The machine-readable aggregate report is written under `.artifacts/` and is not
committed. The command performs local reads only and makes no network or remote
database changes.

## Admission boundary

This result admits the local Chronicle contract to deployment-readiness review.
It does not authorize or prove a Supabase migration, remote canary, collector
publication, authenticated remote read, retry behavior, or production use.
