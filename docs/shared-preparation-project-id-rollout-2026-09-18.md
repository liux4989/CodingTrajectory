# Shared preparation and project-ID rollout, 2026-09-18

The reviewed candidate was deployed and bounded prepared-artifact API
verification passed, including project-ID selection and representative public
detail reads. Legacy SQL fact-data coverage is not applicable to the frozen
artifact-only replacement and was not exercised. No reset, publication,
artifact upload/import, grant change, or collector activation occurred.

## Deployment identity

| Item | Value |
| --- | --- |
| Source commit | `bce03a69d37aa8208268bf287692df4efc7f1e53` |
| Source tree | `08052bc4350fb7f90d6efaf3fdc4a287733e1f12` |
| Dry-run `index.js` SHA-256 | `a33a2b6eaccbd5b059841276d4a85c175dcb5c849eec0ec7c40be787638705b5` |
| Dry-run bundle | 1,799.87 KiB; 170.80 KiB gzip |
| Previous version | `df25862e-d304-4429-bc3f-3eec46d5f8f7` |
| Deployed version | `6a20f5e0-f6b5-425c-93d3-06869e3c3f5e` |
| Deployment UTC | `2026-09-18T05:46:21Z` |

The deployment is active at 100%. Strict remote-conflict checking and top-level
environment selection were explicit. `--keep-vars` preserved dashboard values;
the resulting version retained only `WORKSPACES`, `ARTIFACTS`, `WORKER_VERSION`,
`CT_PRINCIPALS`, and `CT_CURSOR_KEY`. Both replacement gates remain absent.

## Qualification

- Control-plane schema generation and TypeScript checking passed with no tracked
  generated-file change.
- The Wrangler dry run reported the expected Durable Object, R2, and Worker
  version bindings.
- Candidate CLI `production-reader` and `production-collector` identity checks
  passed before deployment with exactly `read` and `collect` roles.
- Loop generated-type check, TypeScript build, and offline Loop/Monitor
  integration passed from the exact candidate.
- The collector launch job remained disabled, with no matching cron or collector
  process.

## Bounded reader verification

The smoke loaded and revalidated the frozen replacement with SHA-256
`34fb957816126e744aaf906e95ebaf17eb7f178904da9c9f5d038ced05243ebd`:
17 graphs, 34 objects, and 8,499,472 bytes. It pinned snapshot 36 and completed
20 production read RPCs without quota, authentication, transport, or snapshot
errors. Together with the two pre-deployment identity checks, this remained well
below the 80-call limit.

Passed production comparisons:

- `project.list` v4 returned one ID-keyed project with the frozen display name;
- `project.sessions` by returned project ID returned 16 visible cards;
- `CodingTrajectory` and `coding-trajectory` convenience selectors returned the
  same full canonical card response as the project-ID selector;
- prepared-card output exactly matched all frozen local summaries;
- the sole remote manifest matched the frozen workspace, publisher, snapshot,
  publication sequence, all 17 graph identities, all 34 object references, and
  8,499,472 referenced bytes.

Observed timings were 949.380 ms for the pinned snapshot, 239.077 ms for project
inventory, 134.061 ms for the manifest, and a 211.223 ms median for 17 immutable
summary reads (range 182.963–711.745 ms). `project.list` took 240.635 ms;
project-ID `project.sessions` took 5,031.883 ms cold, while the two cached
normalized-name calls took 0.853 ms and 0.550 ms.

The harness then asserted that the frozen inventory contained a child session.
Local inspection showed all 17 graphs contain exactly one session and none has a
`parent_session_id`, so this was a harness selection error rather than a remote
response mismatch. The approved continuation marked the child check not
applicable and selected an existing visible root plus actual turn, item, and
event identities from the frozen facts.

The continuation reused the prior evidence and preloaded only the 17 already
verified immutable summaries. Seven public methods then matched full canonical
responses independently dispatched from the frozen local fact set:
`graph.overview`, `graph.stats`, `session.overview`, `session.items`,
`session.events`, `session.summary`, and `session.tree`. Their observed times
were 2,610.597, 7.259, 3.047, 2.393, 2.478, 2.595, and 0.283 ms respectively.
Production hydration used only a pinned snapshot read (781.975 ms), manifest
read (127.586 ms), and selected facts-object read (2,240.330 ms).

The next and fourth continuation RPC was one bounded `ct_fact_read` using the
returned project ID and graph-only kind filter. It retained snapshot 36 but the
harness incorrectly expected the artifact manifest's 17 graphs to appear in the
legacy SQL fact view. The resulting assertion remains recorded as history.

Source inspection corrected its interpretation. `selectGraphs` reads
`records.kind='graph_publication'`, not uploaded immutable graph facts. Normal
legacy fact publication writes `project_id` into that graph-publication
metadata. Artifact replacement instead publishes through `publish_artifacts`;
`commitArtifactPublication` writes `artifact_manifests` and
`artifact_project_publisher`, not legacy `fact_rows` or `graph_publication`
records. The artifact-only replacement is therefore not expected to expose 17
graphs through `ct_fact_read`. Missing `project_id` inside immutable fact bytes
is unrelated to the SQL selector. The SQL selector is neither failed nor passed
by this evidence: legacy SQL fact-data coverage was not exercised.

After that source-grounded correction, exactly three final read-only RPCs ran
without retry: the final pinned snapshot remained 36 (728.862 ms), reader
connection status returned exactly `read` (525.662 ms), and collector connection
status returned exactly `collect` (1,701.006 ms). Both status responses matched
the configured workspace/agent identities and `ct.core.v1` protocol.

The cumulative smoke count is 27 RPCs: 20 initial, four continuation, and three
finalization. The two earlier predeployment connection checks are accounted
separately and also returned exactly `read` and `collect`. Deployment metadata
showed the new version at 100%, unchanged secret names, absent gates, and a
disabled collector.
