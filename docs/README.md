# Documentation

Start with the [product requirements](prd.md) and [architecture](architecture.md).
The private operational-history contract is [Chronicle history](chronicle-history.md).

| Document | Role |
| --- | --- |
| [Chronicle history](chronicle-history.md) | Private operational schema, privacy boundary, replay, and publication bounds |
| [Remote control plane](remote-ct-control-plane-design.md) | Historical, inventory, living, and estimation authorities |
| [Collector handoff](local-collector-handoff.md) | Local collection, delivery recovery, and deployment gates |
| [Private Datahub hosting](datahub-cloudflare-private-hosting.md) | Access-only Cloudflare architecture, route capabilities, and rollout gates; facade implemented locally, not deployed |
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
