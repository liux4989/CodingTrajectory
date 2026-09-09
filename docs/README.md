# Documentation

Start with the [product requirements](prd.md) and [architecture](architecture.md).
The private operational-history design is [Chronicle history](chronicle-history.md).

## Current contracts and operations

| Document | Role |
| --- | --- |
| [Chronicle history](chronicle-history.md) | Private operational schema, privacy boundary, replay, and publication bounds |
| [Chronicle local qualification](chronicle-local-qualification-2026-09-08.md) | Sanitized cross-vendor local admission evidence |
| [Chronicle deployment readiness](chronicle-deployment-readiness-2026-09-09.md) | Non-production admission decision and bounded canary evidence |
| [Chronicle non-production rollout](chronicle-non-production-rollout-2026-09-09.md) | Recorded CT-only reset, exact schema rebuild, and canary publication |
| [Remote control plane](remote-ct-control-plane-design.md) | Historical, inventory, living, and estimation authorities |
| [Private Datahub hosting](datahub-cloudflare-private-hosting.md) | Accepted Access-only Cloudflare architecture, route capabilities, and rollout gates; facade implemented locally, not deployed |
| [Collector handoff](local-collector-handoff.md) | Local collection, delivery recovery, and deployment gates |
| [September 5 rollout](remote-ct-rollout-2026-09-05.md) | Prior Shareable-schema publication evidence; reset and rebuilt as Chronicle on 2026-09-09 |
| [CLI](cli.md), [session API](session-api-redesign.md) | Public usage and progressive evidence retrieval |
| [Plugins](plugin.md) | Executable plugin boundary |
| [Amp collector](amp-collector.md) | Host-local raw capture |
| [Metrics gate](metrics-validation-quality-gate.md), [token glossary](token-usage-glossary.md) | Reconciliation and measurement semantics |
| [Activity reconstruction](codex-activity-reconstruction.md) | Canonical activity and provider-wrapper provenance |
| [Doctor](doctor.md), [invocation log](invocation-log.md) | Local diagnostics and telemetry |

## Feature decisions and research

- [Context window](context-window-redesign.md): implemented diagnosis surface.
- [Dashboard consolidation](dashboard-plugins-redesign.md): accepted consolidation and implementation record.
- [Session graph navigation](session-graph-navigation.md): implemented route restructure record.
- [Amp capture survey](amp-capture-survey-2026-09-05.md) and [plugin mechanism validation](amp-plugin-mechanism-validation.md): bounded Amp acquisition evidence; not a production integration.
- [Datahub design](datahub-redesign.md): draft; does not supersede implemented feature decisions.
- [Agent temporality](agent-temporality.md): original forecasting design; see current estimation modules and contracts for implemented behavior.
- [Query optimization](query-optimization-survey.md) and [incremental benchmark](dashboard-incremental-benchmark.md): dated measurements, not current performance guarantees.

[Archived designs](archive/README.md) retain retired proposals and completed
migration rationale. They are historical context, not implementation instructions.
[Benchmark guidance](../benchmarks/README.md) separates reproducible inputs from
regenerable reports. Git history retains removed reports and earlier attempts.
