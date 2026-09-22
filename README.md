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

## Repeatable local publication

Use `ct collector publish` on the machine holding the private sessions (macOS
or Linux), from a clean, reviewed source checkout. It does not deploy the Worker
or change credentials. Select an existing project and a collector profile with
both read and collect access to the **same workspace**. Do not run another
collector for that project at the same time.
Choose a new `$RUN_DIR` outside the checkout, for example under
`~/.coding-trajectory/publications/`; keep using that directory for recovery.

```bash
uv sync --all-packages --frozen
uv run --frozen --no-sync ct collector publish plan \
  --run-dir "$RUN_DIR" --source-sha "$REVIEWED_COLLECTOR_SHA" \
  --worker-version "$DEPLOYED_WORKER_VERSION" \
  --credential-profile "$COLLECTOR_PROFILE" --workspace-id "$WORKSPACE_ID" \
  --project-id "$PROJECT_ID" --project-name CodingTrajectory \
  --project-root "$LOCAL_PROJECT_ROOT"

# Explicitly authorize this frozen inventory's delivery:
uv run --frozen --no-sync ct collector publish start --run-dir "$RUN_DIR"

# Safe to inspect from a separate terminal while it runs:
uv run --frozen --no-sync ct collector publish status --run-dir "$RUN_DIR"
```

`plan` makes only authenticated read requests, checks the deployed version and
principal, and freezes all discovered complete-line source prefixes with hashes,
file identities and timestamps. There is no age/vendor/session subset filter.
`start` verifies the same checkout SHA/tree and Python version. Changed source
prefixes or changed discovery membership stop before source delivery. Appends
after planning are intentionally left for the next run. Once staged, the exact
publication and artifact bytes live in the run database; resuming that stage
does not rediscover or reprepare newer data.

Before the first artifact upload, preflight checks complete-inventory identity,
zero prepared-method errors, exact 3 MiB RPC size and a conservative compact SQL
manifest projection against the 2 MiB−4096 row guard. Source registration and
checkpoints may already have been accepted by then. The separate 0700 run
directory holds a 0600 database, isolated preparation cache and fsynced audit
receipts. Audit output excludes bodies and tokens; **the database and plan still
contain private content/paths and must not be shared**. Requests have 120-second
per-operation transport timeouts, not a 120-second total-transfer deadline.
Artifact bodies stream in 64 KiB writes with their original `Content-Length`;
bytes, hashes and JSON encoding are unchanged (no HTTP chunked encoding).
Missing objects upload in waves of at most four, with no internal retry loop.
After an error, already-started uploads settle before the run stops; the manifest
is not submitted. SQLite remains on the collector thread and audit writes are serialized.

After any interruption, preserve the directory and reconcile before resuming:

```bash
uv run --frozen --no-sync ct collector publish reconcile --run-dir "$RUN_DIR"
uv run --frozen --no-sync ct collector publish resume --run-dir "$RUN_DIR" \
  --reconciliation-sha "$DIGEST_FROM_RECONCILE"
```

Reconciliation performs only remote reads, leaving the collector database
unchanged. Its immutable report binds to the current local database/audit hashes.
Resume rejects a stale report, repeats authority checks immediately before
writes, and settles already accepted checkpoints without replay. A committed
publication must match the full expanded manifest at its receipt's snapshot;
it is never submitted again. Version, sequence, source-watermark or manifest
disagreements stop for review. This is manifest verification, not full API parity
or runtime performance qualification.

The collector checks authority readiness in batches of up to 512 unique references,
including byte counts and whether an object needs validated index metadata.
Only unexpired completed claims or retained manifest references qualify; local
successful PUT receipts alone never justify skipping an upload. Readiness is
read-only, not a lease: publication revalidates completeness after any expiry or
pruning. Missing objects follow the unchanged authenticated hash/schema/size and
completion checks. Deploy the matching Worker before using this collector; an
unsupported readiness RPC stops the run rather than silently falling back.
Legacy/ad-hoc run directories are not imported automatically. Keep
their receipts and reconcile them using their original pinned tooling rather
than starting a second publication from this command.

## Durable staging release jobs

`npm --prefix cloudflare/control-plane run deploy -- <action> ...` delegates to
`uv run python scripts/deploy-release.py`. No action deploys implicitly.
Use a clean reviewed checkout, `uv sync --all-packages --frozen`,
`uv sync --project cloudflare/control-plane --frozen`, and
`npm --prefix cloudflare/control-plane ci`. The authority is a Python Worker;
see [its development guide](cloudflare/control-plane/README.md) for contract
packaging and local runtime qualification. Keep the run directory **outside
disposable worktrees**, private (0700), and backed up with its append-only audit.

```bash
release="$HOME/.coding-trajectory/releases/<unique-release-name>"
uv run python scripts/deploy-release.py prepare --run-dir "$release" --source-sha <full-reviewed-SHA>
uv run python scripts/deploy-release.py status --run-dir "$release"
uv run python scripts/deploy-release.py verify --run-dir "$release"
# Only authenticated reads. Pause all publishers and other deploy owners first.
uv run python scripts/deploy-release.py preflight --run-dir "$release" --reader-profile <profile>
# After owner approval of the exact returned digest, with scoped account credentials:
uv run python scripts/deploy-release.py deploy --run-dir "$release" --approve-activation <digest>
```

`prepare` seals source/tree, lockfiles, Wrangler configuration and tool versions;
runs the existing metric, collector, connection and prepared-API qualifications;
then seals the complete Python source and WebAssembly dependency tree once.
Release plan version 2 also checks that no extra modules were added to that tree.
Completed phases verify their cached logs and output hashes
instead of rerunning. Interrupted local phases without a sealed receipt rerun;
failed logs remain. Damaged/unsealed evidence fails closed—preserve it rather
than editing a receipt. `verify` also requires the original toolchain and clean
source. This identifies exact bytes, not hermetic rebuilds across machines.

`preflight` checks **all current project manifests in each supplied workspace**
against candidate prepared-method versions and rejects errors/missing indexes.
Repeat `--reader-profile` once per affected workspace. It pins manifest hashes,
endpoint versions and the current single-version staging deployment. `deploy`
repeats those reads and rejects drift before uploading the sealed tree using
its copied configuration and `--strict`, without resolving dependencies again.
Approval is a human authorization requirement; possessing
the digest alone is not an access-control mechanism.

```bash
uv run python scripts/deploy-release.py stop --run-dir "$release"
# Wait for the active owner/child to exit. Local preparation only:
uv run python scripts/deploy-release.py resume --run-dir "$release" --source-sha <same-SHA>
# After any deployment invocation, including a timeout or lost response:
uv run python scripts/deploy-release.py reconcile --run-dir "$release"
```

Stop drains the active command and prevents the next phase; it cannot cancel a
remote commit. Deployment intent is fsynced before invocation. That job **never
replays deployment**, even after a nonzero exit or spawn failure. Reconciliation
makes only reads and matches the active version's exact release message/tag.
`receipt.json` records code activation, phase seconds and operation counts;
it explicitly does not certify application readback. An unmatched result stays
unknown, not “safe to retry”. If another deploy superseded it, inspect version
history before authorizing any new job. There is no automatic rollback.

**Remaining rollout gap:** manifest publication and Worker activation are not
atomic. The descriptor check does not fetch every object, certify semantics,
discover unlisted workspaces, or provide a remote lock. The operator must cover
every affected workspace, pause publishers, review schema/config changes and
perform bounded correct-workspace readback afterward. Empty/new targets fail
closed and require separate bootstrap qualification. For an incompatible API
version, first implement and qualify a bridge reader that supports both stored
formats; prepare replacement data from complete retained facts, publish it with
the existing collector's authority-readiness and idempotency protocol, then
retire the old reader. No bridge or atomic activation is invented by this script.
Do not use `collector publish plan` to rediscover or broaden an approved frozen
recent-only inventory. Retained-fact migration remains separately scoped work;
the release runner never reingests sources, uploads corpus objects, or replays an
accepted publication. Existing collector batches, bounded parallelism, prepared
cache and reconcile-before-resume behavior remain their source of truth.

`.github/workflows/deploy-staging.yml` now performs **build-only qualification**
using the same preparation command. Manual dispatch requires an exact `main`
SHA; pushes and PRs do not deploy. No account or private reader credentials are
injected. Synthetic build evidence is retained for 30 days, including on failure.
It is not a backup for local private release evidence. A downloaded CI job can
only be verified with its pinned toolchain; do not relabel it as a Mac build.
Use a fresh local job when the toolchain differs. CI activation is intentionally
disabled until it can enforce the same live-data compatibility gate without
copying private credentials/corpus into GitHub. Phase timings measure actual
work; reproducibility does not promise instant builds or uploads.

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
