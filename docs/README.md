# Documentation

Start with the [product requirements](prd.md) and [architecture](architecture.md).
The historical publication contract is [Chronicle history](chronicle-history.md),
governed by the [authority boundaries RFC](authority-boundaries.md).

## Current behavior and contracts

| Document | Role |
| --- | --- |
| [CodingTrajectory Loop](loop-design.md) | Local Analytics, evidence references, and future Monitor/Improve boundaries |
| [Chronicle history](chronicle-history.md) | Private operational schema, privacy boundary, replay, and publication bounds |
| [Authority boundaries](authority-boundaries.md) | Accepted evidence, canonical, publication, and query ownership |
| [Remote control plane](remote-ct-control-plane-design.md) | Historical, inventory, and living authorities |
| [Core protocol freeze](core-protocol.md) | Frozen 18-method registry snapshot and the intentional-change gate |
| [Connections](refactor/connections.md) | Connection profile and authentication lifecycle contract |
| [Fact synchronization](refactor/synchronization.md) | Publication sequence, bounds, retry, replacement, and recovery |
| [Collector handoff](local-collector-handoff.md) | Local collection, delivery recovery, and deployment gates |
| [CLI](cli.md) | Public usage and local/remote execution |
| [Plugins](plugin.md) | Executable plugin boundary |
| [Amp collector](amp-collector.md) | Host-local raw capture |
| [Metrics gate](metrics-validation-quality-gate.md) and [token glossary](token-usage-glossary.md) | Reconciliation and measurement semantics |
| [Activity reconstruction](codex-activity-reconstruction.md) | Canonical activity and provider-wrapper provenance |
| [Doctor](doctor.md) and [invocation log](invocation-log.md) | Local diagnostics and telemetry |

## Implemented design records

These record landed changes. Where later intentional changes superseded specific
details, the frozen Core snapshot and current source are authoritative.

| Document | Role |
| --- | --- |
| [Published fact sets refactor](refactor/remote-historical-facts.md) | Clean-break replacement of the artifact/chunk remote historical stack |
| [Direct published facts](refactor/direct-published-facts.md) | Aggregate-free publication implementation and qualification |
| [Indexed historical facts](refactor/fact-index-read-view.md) | Bounded read index, measured cache decision, and staging evidence |
| [Bounded large fact publications](refactor/bounded-large-fact-publications.md) | Measured graph/publication bounds and SQL-backed atomic commit |
| [Session API redesign](session-api-redesign.md) | Progressive evidence retrieval: summary, search, and detail taxonomy |
| [Managed collection](refactor/managed-collection.md) | Superseded: the `ct collector service` supervisor was removed by the fact cutover; retained as qualification evidence |
| [Deferred qualification checklist](refactor/later-qualification-checklist.md) | Items deferred from the initial private deployment; partially superseded |

## Proposals — design only, not operational authorization

| Document | Role |
| --- | --- |
| [Credential registry proposal](refactor/credential-registry-proposal.md) | Reader-first bootstrap, scoped issuance, rotation and registry authority |
| [Upload qualification plan](refactor/upload-qualification-plan.md) | Exact-commit synthetic, capacity and separately authorized canary gates |

## Historical operational evidence

| Document | Role |
| --- | --- |
| [Completed targeted reset](targeted-reset-execution-2026-09-16.md) | Deployment evidence and credential blockers; not an executable reset procedure |
| [Targeted reset recovery plan](targeted-reset-recovery.md) | The executed recovery plan, retained as audit trail for the reset |

Dated rollout evidence, superseded designs, and retired proposals normally remain
in git history. The reset recovery plan and execution record remain linked while
credential bootstrap and upload qualification depend on their deployment
evidence. The temporary reset/recovery runtime — reset code, operations config,
and its qualification script — is historical only and is not part of `main`.

The publication bounds in these documents describe `main`: the larger graph and
publication limits are merged there but not production-qualified or deployed.
The [upload qualification plan](refactor/upload-qualification-plan.md) pins the
deployed clean baseline and the larger candidate as separate lanes; the
deployment identity is as recorded in the 2026-09-16 execution evidence, not a
fresh live check.
[Benchmark guidance](../benchmarks/README.md) separates reproducible inputs from
regenerable reports.
