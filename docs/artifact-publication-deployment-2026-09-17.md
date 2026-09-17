# Immutable artifact publication deployment, 2026-09-17

The reviewed immutable-artifact runtime is deployed at 100%. Credentials,
Durable Object data, the R2 bucket, access grants, and manual collection policy
were preserved. No migration, reset, paid upgrade, or schedule change occurred.

## Frozen source and deployment

| Item | Value |
| --- | --- |
| Integrated evidence commit | `014c0fdea2cda4d1f5f7e17126d61b4ac1f2e9da` |
| Integrated Git tree | `2148b5a4aefcbffbd5451b54e314fbcf71be5fb3` |
| Runtime commit | `fdeb0b1f91f3e740b4df48707edf94329fdc9a8d` |
| Dry-run `index.js` SHA-256 | `9ccd6582da2fca87110f886456088e8ee992985787bd896e366942d274421290` |
| Dry-run `index.js` bytes | 1,829,878 |
| Previous active version | `738480ff-10ea-47c8-844d-d888198702b2` |
| Deployed version | `27178c5e-650d-4009-8b9e-e0770fc07657` |
| Version tag | `artifact-fdeb0b1` |
| Deployment UTC | `2026-09-17T11:09:58.382Z` |

Origin `main` had no competing commits and was fast-forwarded from `b2ad3a1` to
the reviewed evidence commit without rewriting history. Before deployment,
TypeScript/schema generation, targeted Ruff lint and format checks, all committed
metric baselines, the metrics quality gate, and whitespace checks passed. The
Wrangler dry run reported the expected `WORKSPACES`, `ARTIFACTS`, and
`WORKER_VERSION` bindings.

The production deploy used the top-level Wrangler environment, strict remote
conflict checking, and preserved existing variables. The active version retains
the `CT_PRINCIPALS` and `CT_CURSOR_KEY` secrets and the existing
`coding-trajectory-artifacts` bucket. R2 remained at 87 objects / 3.09 MB before
and immediately after deployment.

## Minimal live checks and quota stop

The `production-reader` and `production-collector` profiles both authenticated,
matched their configured identities and protocol, and retained exactly their
`read` and `collect` roles. The first and only post-deployment data call returned
HTTP 503 with `database_read_quota_exceeded` for `ct_workspace_snapshot`.
No further data reads were attempted after that quota response.

The only authorized real publication input was the previously reviewed
CodingTrajectory pilot export with SHA-256
`bb64366e3c4c4a4cb7c078fd236bcf7c1d5e4f162c1647732114ae18632853c0`.
That private export remains unavailable locally. No current, wider, raw, or
synthetic inventory was substituted, and no real publication was attempted.
Snapshot, retained-manifest, graph, retry, and changed-publication verification
remain blocked until quota access recovers and the exact reviewed input is
available where publication is required.

Local workerd benchmarks and qualifications remain evidence about implementation
behavior only; they are not production billing or capacity evidence.
