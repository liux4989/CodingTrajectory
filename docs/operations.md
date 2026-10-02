# Operations guide

Capture, publication, and deployment are separate operations. A local query or
build does not authorize remote writes. Use reviewed source and matching Worker,
reader, and collector contracts.

## Capture Amp logs

The Amp project plugin is `.amp/plugins/coding-trajectory/index.ts`.
It writes private append-only journals to
`~/.coding-trajectory/amp/sessions/T-<thread-id>.jsonl`.
`CT_AMP_LOG_DIR` selects another directory.

Journals contain thread metadata, message revisions, and live observations.
Ingestion uses the latest revision for each stable ID. The plugin reconciles
full paged transcripts and records agent/tool hooks. An orb stores its own
journals; files do not synchronize between orbs.

Use only these versioned journals as Amp ingestion input. `amp threads export`
uses different IDs and is not interchangeable. Successful, matched live
`create_thread` evidence can establish a spawn. Read, message, and wait references
do not establish parentage. Separate cross-orb publications do not merge into a
complete cross-host graph.

Amp capture does not report provider tokens, billed cost, or exact inference
timing. Hook timestamps describe local observation. See the
[Amp throughput estimate](token-usage-glossary.md#amp-observed-throughput-estimate).

**Warning:** the plugin can launch a publication executable after reconciliation
and completed turns. Set `CT_AMP_AUTO_PUBLISH=0` for capture-only operation.
The default executable is `~/.coding-trajectory/bin/run-chronicle-collector`;
`CT_AMP_PUBLISH_COMMAND` can select another absolute executable path.
Installing or enabling that executable requires separate publication authorization.
Never upload the raw journals.

Offline capture qualification:

```sh
uv run python scripts/validate-amp-live.py
```

## Publish a complete project inventory

**Warning:** publication writes private retained content to the selected workspace.
Read the [privacy boundary](architecture.md#evidence-and-privacy) first.
Do not run overlapping collectors for the same project.

The repeatable runner requires an existing project, a clean reviewed checkout,
and a matching deployed Worker. The collector needs `collect`; verification
needs `read` in the same workspace and endpoint. One combined profile can serve both.
Configure profiles through the [connection commands](cli.md#configure-a-connection).

Choose a new private `$RUN_DIR` outside the checkout, for example under
`~/.coding-trajectory/publications/`. Keep it for all recovery actions.
Set each variable below to the reviewed source, authority, and project values.

1. Install the locked dependencies:

   ```sh
   uv sync --all-packages --frozen
   ```

2. Freeze and inspect the inventory:

   ```sh
   uv run --frozen --no-sync ct collector publish plan \
     --run-dir "$RUN_DIR" --source-sha "$REVIEWED_COLLECTOR_SHA" \
     --worker-version "$DEPLOYED_WORKER_VERSION" \
     --credential-profile "$COLLECTOR_PROFILE" --reader-profile "$READER_PROFILE" \
     --workspace-id "$WORKSPACE_ID" --project-id "$PROJECT_ID" \
     --project-name "$PROJECT_NAME" --project-root "$LOCAL_PROJECT_ROOT"
   ```

3. After explicit authorization, start delivery:

   ```sh
   uv run --frozen --no-sync ct collector publish start --run-dir "$RUN_DIR"
   ```

4. Inspect progress from a separate terminal:

   ```sh
   uv run --frozen --no-sync ct collector publish status --run-dir "$RUN_DIR"
   ```

`plan` makes authenticated reads only. It freezes complete-line source prefixes,
hashes, file identities, and discovery membership. It has no age, vendor, or
session subset filter. `start` checks source SHA/tree, Python version, and frozen
input. Later appends belong to the next run.

Staged artifact bytes and requests remain in the run database. Resume does not
discover or prepare newer sources. Directories use mode 0700; databases use 0600.
**Do not share the plan or database:** they contain private paths and content.
Audit receipts exclude request bodies and tokens.

### Publication recovery

After any interruption, preserve the run directory.

1. Reconcile the retained run against remote state:

   ```sh
   uv run --frozen --no-sync ct collector publish reconcile --run-dir "$RUN_DIR"
   ```

2. After reviewing the report, resume with its fresh digest:

   ```sh
   uv run --frozen --no-sync ct collector publish resume --run-dir "$RUN_DIR" \
     --reconciliation-sha "$DIGEST_FROM_RECONCILE"
   ```

Reconciliation makes remote reads and leaves the collector database unchanged.
Its report binds to current database and audit hashes. Resume rejects stale
reports, rechecks authority before writes, and settles accepted checkpoints.
A committed publication is verified against its exact manifest; it is not resubmitted.
Version, sequence, watermark, or manifest disagreement stops the run for review.

Object readiness is not a lease. Publication revalidates references after expiry
or pruning. A successful local PUT receipt alone cannot justify skipping upload.
Missing objects use bounded transfers; mixed outcomes stop manifest submission.
Requests have a 120-second per-operation timeout, not a total-transfer deadline.

Current artifact limits are 16 MiB per facts object, 4 MiB per summary, and
512 graphs per publication. Prepared API requests allow 64 KiB; responses allow
448 KiB. Oversize or unsupported results fail explicitly, not by silent truncation.
Publication preflight checks request and stored-manifest bounds before artifact upload.
Accepted source registration or checkpoints can precede that check.

Do not import legacy/ad-hoc run directories into this runner. Recover them with
their original pinned tooling. Pending legacy SQL publications remain in the
outbox with an error; they are not deleted or converted automatically.

## Prepare and deploy a release

**Warning:** deployment changes shared code. Obtain explicit authorization for the
target environment. Pause publishers during activation to prevent manifest drift.
Deployment does not upload, reset, or delete application data.

[RELEASE.md](../RELEASE.md) controls CI candidate preparation. Advance `release_id`
by exactly one in a reviewed final commit. Release 0 never deploys.
Both Core CI jobs must pass before marker-driven preparation.
CI qualification is build-only; it does not activate a Worker.

Use a clean reviewed checkout. Install the root and Worker locked environments
and the Worker's Node dependencies as described in the
[Worker guide](../cloudflare/control-plane/README.md#local-development).
Choose a private, backed-up `$RELEASE_DIR` outside disposable worktrees.
Set `$TARGET` to `staging` or `production` explicitly.

1. Prepare and seal the reviewed source:

   ```sh
   uv run python scripts/deploy-release.py prepare \
     --run-dir "$RELEASE_DIR" --source-sha "$REVIEWED_SOURCE_SHA"
   ```

2. After authorization, activate the sealed release:

   ```sh
   uv run python scripts/deploy-release.py deploy --environment "$TARGET" \
     --run-dir "$RELEASE_DIR" --reader-profile "$READER_PROFILE"
   ```

Preparation seals source/tree, locks, configuration, tool versions, qualification
logs, Python modules, and WebAssembly dependencies. Completed phases verify
their receipts instead of rerunning. Preserve damaged evidence; do not edit receipts.
Verification requires the original pinned toolchain. This is exact-byte identity,
not a promise of identical rebuilds on other machines.

Deploy runs compatibility preflight, one activation, and smoke reads.
Repeat `--reader-profile` for every affected workspace. Preflight checks all
current project manifests in supplied workspaces and rejects incompatible data.
Deploy repeats these reads and rejects drift. An empty workspace needs no data bootstrap.

For separate approval, run `preflight` with the same environment, run directory,
and reader profiles. Then use `deploy --approve-activation DIGEST`.
The digest identifies reviewed state; it is not an authorization credential.

### Release recovery

- `status` reports recorded progress, not current remote health.
- `stop` drains the active command and blocks the next phase. It cannot cancel
  a remote commit. Wait for the active owner/child to exit.
- `resume --source-sha SAME_SHA` resumes local preparation only.
- After any deployment invocation, timeout, or lost response, run `reconcile`
  with the same environment and run directory. Do not replay deployment.
- If activation succeeded but readback failed, run `smoke` to retry reads.
  Do not redeploy to repair a smoke failure.

Activation intent is durable before invocation. A job never retries activation,
including after a spawn failure or nonzero exit. Unmatched outcomes stay unknown.
If another release superseded the target, inspect version history before
authorizing another job. There is no automatic rollback.

Prepare once to promote the same sealed release to another environment.
Each environment has its own preflight, activation intent, and receipts.
Code rollback does not roll back SQLite or R2 writes.
Routine releases never reset data; temporary reset and cleanup endpoints are retired.
