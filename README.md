# coding-trajectory

Unified canonical models and CLI tooling for coding-agent session graphs.

## CodingTrajectory Loop

Loop is the local-first Analytics product: explore local sessions, open an
investigation, and resolve stable item/event references back to canonical evidence.
It saves reading positions, never copied transcripts. The deterministic Monitor
foundation (turn-token-budget watches, dry-run, and finding triage) is included;
broader Monitor automation, Improve, and hosted delivery are deferred. See
[Loop design and local usage](docs/loop-design.md).

```bash
uv sync --all-packages --locked
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

## Layering

- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources. They preserve agent-agnostic facts and stable references reconstructed from vendor logs.
- `SessionGraph` preserves unified session lineage internally. The CLI exposes ordinary human forks through `ct session tree` and each branch's orchestration run through `ct session graph`; forked conversations are not aggregated as spawned agents.
- Presentation-oriented interpretations such as replay sections, UI workflows, and enrichment-specific labels do not belong in the core layer.

## Project identity and prepared reads

`project.list` v4 returns an `items` map keyed by opaque project ID. Each value
contains `project_id`, `display_name`, `path`, and `vendors`. Pass the returned ID
to `project.sessions` v4 as `project_id` (CLI: `ct project sessions --project-id ID`).
Names remain a convenience selector, not identity; ambiguous names must use an ID.
Local IDs identify canonical host locations, so distinct directories with the
same name remain distinct. Moving a directory changes its local ID. Remote IDs
come from the workspace registry and survive display-name changes. IDs are
authority-scoped: do not substitute a local ID for a remote ID. Temporary local
locations are listed individually rather than combined under a synthetic project.

Local reads and the collector share graph preparation. Canonical graph content
and the preparation version key a private disposable SQLite cache at
`~/.coding-trajectory/prepared-graphs.sqlite` (128 MiB of cached payloads).
Unchanged graphs reuse projected facts and session cards across processes;
source discovery/parsing and content hashing still run to detect unpublished
changes. The first read of changed data may prepare it locally. Publication
uses the same preparation and applies transport bounds without truncating local
reads. List filters are shared; remote project selection uses manifest ownership
and reads summaries only for that project. Detailed remote reads remain lazy.

The existing zero-item visibility rule is unchanged. Project IDs are attached
at read time, so this refactor does not require resetting remote data or changing
existing immutable facts/summaries. No publication is automatic.

## Artifact-only remote history and legacy cleanup

Remote historical reads and collector publication use immutable artifacts only.
The SQL fact read/stage/publish RPCs are retired (HTTP 404); there is no automatic
SQL fallback when artifact snapshots are unavailable. Upgrade collectors and
readers together. Pending legacy collector publications stop delivery with an
actionable error and remain in the local outbox; they are never silently deleted
or republished. Historical benchmark records describe their original versions.

Retirement does not drop production tables by default. Operators can inspect
`ct_legacy_fact_cleanup_status` with a reader credential. After confirming the
target workspace, snapshot, artifact manifest, disabled collection, zero legacy
publication records, and empty legacy data tables, an explicitly authorized
deployment may temporarily set `CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID` to that
workspace UUID. On the target Durable Object's next initialization, its object
ID is checked and a synchronous transaction drops only `fact_rows`, `fact_schema`,
`staged_fact_rows`, `staged_fact_items`, `staged_fact_generations`, and
`validated_fact_graphs`. The only allowed nonempty table is `fact_schema`, with
the single known marker `(id=1, version=1)`. Unexpected contents abort cleanup.

This is a deployment-controlled schema migration, not a reader deletion API.
Deploy retirement with the gate absent first; inspect before enabling it. Remove
the gate immediately after cleanup (or on failure), then confirm the six tables
remain absent, the snapshot and artifact reads are unchanged, and roles still
match. Shared SQL metadata, receipts, manifests, and R2 objects are preserved.
Do not roll back to a pre-retirement Worker: it would recreate the old tables.
Use `node scripts/qualify-legacy-fact-cleanup.mjs` for disposable workerd/SQLite
qualification of table guards, target isolation, gate teardown, and retry.

## Staging deployment CI

`.github/workflows/deploy-staging.yml` is manual-only and deploys code, not session
data. It checks the full supplied commit SHA against the selected `main` ref,
installs locked dependencies with pinned actions/tool versions, runs metric and
prepared-publication qualifications locally, and builds the staging Worker once.
The deploy job verifies the immutable artifact's hashes and source/configuration
identity, then uploads that same bundle with `--no-bundle --env staging --strict`.
It records source/tree/tool versions, bundle checksums, deployment output and
before/after remote status in run artifacts retained for 30 days. This makes the
promoted artifact identifiable; it does not claim hermetic, bit-for-bit rebuilds
across GitHub runner image updates or certify live application/data behavior.

One-time repository setup (not performed by adding the workflow):

1. Create the GitHub Environment **`staging`**. Require a reviewer, prevent
   self-review and restrict deployment branches to `main`. An environment name
   in YAML alone does **not** enforce approval; configure protection before
   adding credentials. Availability depends on the repository's GitHub plan.
2. Add environment-only secrets `CLOUDFLARE_ACCOUNT_ID` and
   `CLOUDFLARE_API_TOKEN`. Scope the token to the existing staging account and
   the permissions needed to deploy/read the existing Worker and its bindings.
   Do not add unrelated account-administration privileges or copy collector/
   principal credentials into CI. Existing `CT_PRINCIPALS` and `CT_CURSOR_KEY`
   stay in Cloudflare; token capabilities may be broader than this workflow.
3. After the reviewed workflow/source reaches `main`, choose **Actions → Deploy
   control plane to staging → Run workflow**, select `main` and supply its full
   reviewed SHA. Inspect the build provenance and approve the environment job.
   Do not overlap this with a manual deployment or an active collector run;
   workflow concurrency only serializes this workflow, not external operators.

Neither pushes nor pull requests deploy. Workflow reruns are rejected, including
“rerun failed jobs”: a failed/timeout deployment may already have committed.
Preserve the evidence and reconcile remote status read-only before approving a
new dispatch. There is no automatic rollback; SQL changes may be roll-forward
only. The workflow applies the reviewed staging Wrangler configuration but does
not provision credentials, publish private sessions, replay outboxes, run remote
benchmarks, or switch production traffic. Collector publication and bounded
correct-workspace read-back remain separately authorized operations.

## Docs

- [Documentation index](docs/README.md)
- [Product requirements](docs/prd.md) and [architecture](docs/architecture.md)
- [Chronicle operational history](docs/chronicle-history.md)
- [CLI usage](docs/cli.md)
- [Collector and deployment handoff](docs/local-collector-handoff.md)
- [Benchmark and artifact policy](benchmarks/README.md)

## Checks

- `uv run ruff check .` for repo-wide Python static analysis
- `bun run --cwd packages/plugins/loop/web check` for generated Core consumer types and TypeScript
- `uv run python scripts/check-loop.py` for offline local HTTP integration
- `uv run python scripts/check-core-protocol.py` for the frozen Core boundary
