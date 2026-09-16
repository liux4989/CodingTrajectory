# Documentation

Start with the [product requirements](prd.md) and [architecture](architecture.md).
The historical publication contract is [Chronicle history](chronicle-history.md),
governed by the [authority boundaries RFC](authority-boundaries.md).

| Document | Role |
| --- | --- |
| [CodingTrajectory Loop](loop-design.md) | Local Analytics, evidence references, and future Monitor/Improve boundaries |
| [Chronicle history](chronicle-history.md) | Private operational schema, privacy boundary, replay, and publication bounds |
| [Authority boundaries](authority-boundaries.md) | Accepted evidence, canonical, publication, and query ownership |
| [Direct published facts](refactor/direct-published-facts.md) | Aggregate-free publication implementation and qualification |
| [Indexed historical facts](refactor/fact-index-read-view.md) | Bounded read index, measured cache decision, and staging evidence |
| [Bounded large fact publications](refactor/bounded-large-fact-publications.md) | Measured graph/publication bounds and SQL-backed atomic commit |
| [Remote control plane](remote-ct-control-plane-design.md) | Historical, inventory, and living authorities |
| [Collector handoff](local-collector-handoff.md) | Local collection, delivery recovery, and deployment gates |
| [CLI](cli.md) and [session API](session-api-redesign.md) | Public usage and progressive evidence retrieval |
| [Plugins](plugin.md) | Executable plugin boundary |
| [Amp collector](amp-collector.md) | Host-local raw capture |
| [Metrics gate](metrics-validation-quality-gate.md) and [token glossary](token-usage-glossary.md) | Reconciliation and measurement semantics |
| [Activity reconstruction](codex-activity-reconstruction.md) | Canonical activity and provider-wrapper provenance |
| [Doctor](doctor.md) and [invocation log](invocation-log.md) | Local diagnostics and telemetry |

Dated rollout evidence, superseded designs, and retired proposals are removed
from the tree; git history retains them.
[Benchmark guidance](../benchmarks/README.md) separates reproducible inputs from
regenerable reports.
