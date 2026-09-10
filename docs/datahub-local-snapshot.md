# Publish a local seven-day Datahub snapshot

Datahub can be deployed as the web app plus precomputed JSON responses. The
snapshot Worker uses only its bundled Static Assets and Cloudflare Access.
The snapshot has no database or remote publisher dependency.
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
as Worker secrets.

The exporter defaults to this repository. `--project` can select another local
project explicitly. `--assets` selects an existing hosted build directory. The
snapshot files are generated under the ignored `web/dist/_snapshot` directory;
they must not be committed. Build first: the web build clears the output folder.

## Query protocol and validation

Local and hosted Datahub use one `POST /api/datahub/query` endpoint with the
`ct.datahub.v1` envelope. The hosted snapshot supports the
`datahub.snapshot`, `datahub.changes`, `projects`, `sessions`, `session.graph`,
`session.tree`, and metadata-only `session.items` methods. Optional filters may
be explicit `null`. Missing or unsupported method data is returned as `null`
with a structured availability reason; invalid required parameters remain an
error. Lists retain filtering and revision-pinned pagination. After
republishing, an old cursor fails with 409 and changes requests tell the client
to reset. All assets and API responses require Access and use
`Cache-Control: no-store`.

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

The optional Cloudflare control plane is independent of this snapshot build
and Worker. No remote database is read during export.
