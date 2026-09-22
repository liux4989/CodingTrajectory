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

## Artifact-only remote history

Remote historical reads and collector publication use immutable artifacts only.
The SQL fact read/stage/publish RPCs are retired (HTTP 404); there is no automatic
SQL fallback when artifact snapshots are unavailable. Upgrade collectors and
readers together. Pending legacy collector publications stop delivery with an
actionable error and remain in the local outbox; they are never silently deleted
or republished. Historical benchmark records describe their original versions.

The one-time legacy reset and table cleanup are complete. Their endpoints,
deployment flags, and helper scripts are retired. Routine deployments never
modify or delete application data. Historical qualification records are retained
as evidence of their original versions.

## Repeatable local publication

Use `ct collector publish` on the machine holding the private sessions (macOS
or Linux), from a clean, reviewed source checkout. It does not deploy the Worker
or change credentials. Select an existing project and a collector profile with
collect access and a reader profile with read access to the **same workspace**.
A combined read+collect profile can serve both roles. Do not run another
collector for that project at the same time.
Choose a new `$RUN_DIR` outside the checkout, for example under
`~/.coding-trajectory/publications/`; keep using that directory for recovery.

```bash
uv sync --all-packages --frozen
uv run --frozen --no-sync ct collector publish plan \
  --run-dir "$RUN_DIR" --source-sha "$REVIEWED_COLLECTOR_SHA" \
  --worker-version "$DEPLOYED_WORKER_VERSION" \
  --credential-profile "$COLLECTOR_PROFILE" --reader-profile "$READER_PROFILE" \
  --workspace-id "$WORKSPACE_ID" \
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

[`RELEASE.md`](RELEASE.md) is the batch-release ledger. Ordinary commits and
release-note edits run Core CI without preparing or activating a release. A
reviewed final commit advances `release_id` by exactly one; after both Core CI
jobs pass, that transition alone prepares a sealed candidate for the selected
`staging` or `production` target. Decreasing or skipping IDs, changing the target
without an ID increment, or introducing a nonzero marker without the zero
baseline fails CI. Release `0` never deploys.

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
# Explicit deploy performs preflight, one activation, then smoke validation.
uv run python scripts/deploy-release.py deploy --environment staging --run-dir "$release" --reader-profile <profile>
```

`prepare` seals source/tree, lockfiles, Wrangler configuration and tool versions;
runs the existing metric, collector, connection and prepared-API qualifications;
then seals the complete Python source and WebAssembly dependency tree once.
Release plan versions 2 and 3 also check that no extra modules were added to that tree.
Completed phases verify their cached logs and output hashes
instead of rerunning. Interrupted local phases without a sealed receipt rerun;
failed logs remain. Damaged/unsealed evidence fails closed—preserve it rather
than editing a receipt. `verify` also requires the original toolchain and clean
source. This identifies exact bytes, not hermetic rebuilds across machines.

`preflight` checks **all current project manifests in each supplied workspace**
against candidate prepared-method versions and rejects errors/missing indexes.
Repeat `--reader-profile` once per affected workspace. It pins manifest hashes,
endpoint versions and the current single-version target deployment. `deploy`
repeats those reads and rejects drift before uploading the sealed tree using
its copied configuration and `--strict`, without resolving dependencies again.
Approval is a human authorization requirement; possessing
the digest alone is not an access-control mechanism.

```bash
uv run python scripts/deploy-release.py stop --run-dir "$release"
# Wait for the active owner/child to exit. Local preparation only:
uv run python scripts/deploy-release.py resume --run-dir "$release" --source-sha <same-SHA>
# After any deployment invocation, including a timeout or lost response:
uv run python scripts/deploy-release.py reconcile --environment staging --run-dir "$release"
```

Stop drains the active command and prevents the next phase; it cannot cancel a
remote commit. Deployment intent is fsynced before invocation. That job **never
replays deployment**, even after a nonzero exit or spawn failure. Reconciliation
makes only reads and matches the active version's exact release message/tag.
The activation receipt records code activation, phase seconds and operation counts.
Successful `deploy` also records a smoke receipt and returns `smoke_passed`.
If readback fails after activation, rerun `smoke`; never redeploy just to retry reads.
`status` reports the latest recorded stage, not current remote health. An unmatched result stays
unknown, not “safe to retry”. If another deploy superseded it, inspect version
history before authorizing any new job. There is no automatic rollback.

Deployment and data publication remain separate operations. Preflight checks the
supplied workspaces; publishers must be paused during activation to avoid drift.
Smoke validation checks a representative prepared read, or confirms the authenticated
empty state when a workspace has no manifests. Empty workspaces need no data upload
or separate bootstrap workflow. Incompatible stored data still blocks activation;
resolve that mismatch as separately scoped work before deploying.
Do not use `collector publish plan` to rediscover or broaden an approved frozen
recent-only inventory. Retained-fact migration remains separately scoped work;
the release runner never reingests sources, uploads corpus objects, or replays an
accepted publication. Existing collector batches, bounded parallelism, prepared
cache and reconcile-before-resume behavior remain their source of truth.

`.github/workflows/deploy-staging.yml` is a reusable **build-only qualification**
called by Core CI only after a valid `release_id` transition and successful
validation of the exact `main` SHA. No manual workflow dispatch can bypass the
marker. No account or private reader credentials are injected. Synthetic build
evidence is retained for 30 days, including on failure.
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

### Deploy and sync independently

A release is prepared and qualified once. Promote the same sealed artifact using
`--environment staging` or `--environment production` for `preflight`, `deploy`,
`reconcile`, and `smoke`. Each environment has its own activation intent and receipt.
Preflight checks target bindings, credentials, deployed version, and data compatibility.
Deployment automatically checks authentication and a representative prepared read.
The separate `smoke` action remains available for retrying those reads. Deploy never resets or uploads data.

Data sync remains a separate collector operation. `collector publish plan` accepts
`--reader-profile` when the collector credential only has collect permission; both
profiles must target the same workspace and endpoint. Existing registrations and
unchanged checkpoints are reused by the collector.

Artifact uploads group up to 32 objects of at most 64 KiB each, with at most 512 KiB
of object bytes per request. A bounded binary header precedes the exact original
bytes. Larger objects use the single-object path. Four rolling transfer slots feed
the Worker, which performs at most four storage operations concurrently per batch.
Every object retains its hash, schema, claim, completion check, and storage key.
Mixed outcomes stop publication; reconcile and check readiness before resending
missing objects. Uploaded objects become visible only through an accepted manifest.

Ordinary single-object telemetry is buffered and flushed every 32 completions or
readiness page. Batches persist request-level evidence. Publication intent, failures,
and recovery information remain durable; telemetry is never retention authority.

The completed legacy reset tooling is retired. See [reset retirement](docs/artifact-replacement-workflow.md).

For a repeatable retained-object transport comparison, run
`scripts/benchmark-artifact-upload.py --database <accepted-collector.sqlite>
--output <private-report.json>` to inspect grouping. Add `--execute --profile <collector>
--worker-version <UUID> --transport single|batch --limit 512` for a bounded warm
comparison. Both runs use the same frozen object bytes; the benchmark refuses missing
objects, never resets a workspace, and never publishes a manifest. Cold R2 insertion
latency is not measured by this comparison.

For an independently reviewed preflight, use `preflight --reader-profile <profile>`
then `deploy --approve-activation <digest>` with the same explicit environment and
run directory. Normal deployment accepts reader profiles directly; both paths keep
target checks, durable activation intent, and uncertain-outcome reconciliation.
