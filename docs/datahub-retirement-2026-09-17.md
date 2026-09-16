# Legacy Datahub Cloudflare retirement — 2026-09-17

The owner explicitly approved deleting the old Cloudflare Datahub deployment
after removal of its tracked source from `main`. The two legacy Workers and their
dedicated Access applications were deleted. The Chronicle control plane was not
redeployed. Loop remains the local replacement; no hosted Loop was deployed.

## Deleted resources

Account: `b3f2d220197bf66837f1e05c240cf8b1`.

| Resource | Identity | Result |
| --- | --- | --- |
| Worker | `coding-trajectory-datahub-live` | DELETE 200; absent from subsequent Worker list |
| Worker | `coding-trajectory-datahub-preview-candidate` | DELETE 200; absent from subsequent Worker list |
| Access application: CodingTrajectory Datahub live | `9672fb26-71d4-4e36-8ce5-3ff97f4eb8cc` | DELETE 202; absent from subsequent complete application list |
| Access application: CodingTrajectory Datahub candidate | `6c310a9d-5275-490c-b390-74f484e4d018` | DELETE 202; absent from subsequent complete application list |

Preflight found only assets and secrets on the old Workers, no storage bindings
or custom domains. Settings from all ten account Workers contained no service or
script binding to either target. Each deleted Access application's domain and
destinations belonged solely to its matching retired Worker hostname. Workers
were deleted with `force=false`, followed by the two dedicated Access apps.
Shared Access policies and unrelated applications were not deleted.

After deletion, the live `workers.dev` URL returned HTTP 404 without a redirect.
The preview URL probe returned a connection error, not an HTTP status; deletion
of that Worker was confirmed through the authenticated resource inventory.

## Retained control plane and reader verification

- Worker: `coding-trajectory-control-plane`, version
  `df81dc0b-6906-4a18-b9c2-bb87e39d4a4a`, still receiving 100% of traffic.
- WORKSPACES: `867b720fe75947219811344c33645219`, class `Workspace`.
- ARTIFACTS: `coding-trajectory-artifacts`.
- Secret and metadata bindings remained present: `CT_PRINCIPALS`, `CT_CURSOR_KEY`
  and `WORKER_VERSION`.
- At 2026-09-16 17:50:14 UTC (2026-09-17 Asia/Shanghai), the `production-reader`
  returned HTTP 200 for connection status, workspace snapshot and project
  inventory. Every response carried the expected version and workspace identity;
  the role was exactly `read`, snapshot sequence was **0**, and inventory was
  **empty**.

Deleting the old Worker's secret bindings does not revoke its token in Chronicle.
The existing reader grant and Mac `production-reader` Keychain credential remain
in use. No Core registry change, reset, registration, publication, heartbeat or
schedule enablement occurred during this retirement.

The accepted next plan is [three internal rollout jobs](refactor/upload-qualification-plan.md)
with [collect/read credentials](refactor/credential-registry-proposal.md). A valid
collector cutover, exact build selection and bounded upload remain to be executed;
this cleanup does not qualify the larger runtime's maximum capacity.
