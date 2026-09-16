# Qualification after the first deployment

Deferred by agreement on 2026-09-10. These items do not block the initial private
deployment. They remain required before enabling the corresponding capability or
claiming broader recovery, scale, retention, or freshness guarantees.

Status note (2026-09-16 cleanup): the Q-numbers reference the original
qualification review record, which is not retained in this tree. The "16 MiB
compatibility ceiling" named below was the publication/request bound at the
time; [Bounded large fact publications](bounded-large-fact-publications.md)
later replaced it on `main` (16 MiB per graph, 96 MiB per publication, 3 MiB
request body) — merged, but not production-qualified or deployed. Deployment
qualification of those larger bounds remains open under the
[upload qualification plan](upload-qualification-plan.md); the other deferred
items stand.

- [ ] Complete remote restore, source rotation/truncation, parser upgrade, and
  transient-failure injection coverage (Q02, Q05-Q09, Q18-Q19).
- [ ] Qualify large graphs, early insertion, and long active turns; replace the
  16 MiB compatibility ceiling and full-prefix parsing before promising larger or
  append-only processing (Q03-Q04).
- [ ] Implement and qualify reference-safe garbage collection, staging retention,
  cursor expiration, and resnapshot behavior before enabling cleanup (Q13, Q20).
- [ ] Qualify multi-host ownership, delayed replay, presence, sustained load, and
  outage catch-up with an explicitly recorded workload envelope and source counts.
- [ ] Review older publications that lack canonical detail projections; refresh
  them through explicit collector publication when their details are needed.

Keep manual publication and existing storage budgets for the initial release.
The collector schedule remains paused. Do not infer completed checks from this
deferral; record measured evidence as each item is finished.
