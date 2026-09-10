# Publish a local seven-day Datahub snapshot

Datahub can be deployed as the web app plus precomputed JSON responses. The
snapshot Worker uses only its bundled Static Assets and Cloudflare Access.
There is no Supabase, D1, R2, remote publisher, or Python Worker dependency.
Python runs locally to discover sources, sanitize Chronicle artifacts, replay
them, and validate the existing Datahub response contracts before upload.

The export covers the current project's local seven-day discovery window and
retains graph history required by those sessions. This is a frozen view captured
at `generated_at`, not a continuously advancing seven-day remote query. To
refresh it, rebuild and republish. Raw logs and evidence endpoints are excluded;
bounded Chronicle narrative previews and item metadata are retained.

## Build and publish

From the repository root:

```sh
npm --prefix packages/plugins/datahub/web run build:hosted
uv run python scripts/export-datahub-snapshot.py
uv run python scripts/qualify-datahub-snapshot.py
npm --prefix packages/plugins/datahub/web run check:worker
packages/plugins/datahub/web/node_modules/.bin/wrangler deploy --config packages/plugins/datahub/wrangler.snapshot.jsonc
```

Wrangler uses your existing Cloudflare login. The snapshot config targets the
private candidate hostname. Its existing Access application must continue to
cover the entire hostname with the owner-only policy. The Worker requires only
`CF_ACCESS_TEAM_DOMAIN` and that application's `CF_ACCESS_AUD`, provisioned once
as Worker secrets. Do not use Supabase credentials or the old Datahub release
runner for this deployment.

The exporter defaults to this repository. `--project` can select another local
project explicitly. `--assets` selects an existing hosted build directory. The
snapshot files are generated under the ignored `web/dist/_snapshot` directory;
they must not be committed. Build first: the web build clears the output folder.

## Behavior and validation

The seven supported routes are snapshot, changes, projects, sessions, graph,
tree, and metadata items. Lists retain filtering and revision-pinned pagination.
After republishing, an old cursor fails with 409 and changes requests tell the
client to reset. Unsupported routes and content hydration remain unavailable.
All assets and API responses require Access and use `Cache-Control: no-store`.

The exporter verifies canonical Chronicle replay and validates API response
models. Each exported response has a digest in the manifest and a 20 MiB size
ceiling. The qualification script runs the actual route adapter and generated
assets inside local workerd, compares graph/tree responses for every session,
checks pagination and filtering, and checks the deployed entrypoint's signed-out
denial. Its local harness is temporary and is never included in deployment.

A successful upload proves deployment; local qualification is separate from an
authenticated live browser check. Record the deployed version, captured revision,
and remaining browser verification in the task report. A Worker deployment
versions code and assets together; rollback restores the previous version and
snapshot together, with no database rollback or data migration.

This direct snapshot approach replaces the proposed D1/R2 migration for the
current publishing task. The existing Supabase infrastructure is not used or
modified by the snapshot build or Worker.
