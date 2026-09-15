# Qualification after the first deployment

Deferred by agreement on 2026-09-10. These items do not block the initial private
deployment. They remain required before enabling the corresponding capability or
claiming broader recovery, scale, retention, or freshness guarantees.

- [ ] Complete remote restore, source rotation/truncation, parser upgrade, and
  transient-failure injection coverage (Q02, Q05-Q09, Q18-Q19).
- [ ] Qualify large graphs, early insertion, and long active turns; replace the
  8 MiB compatibility ceiling and full-prefix parsing before promising larger or
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
