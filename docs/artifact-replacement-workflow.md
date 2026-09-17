# Workspace-scoped artifact replacement

Status: **offline tooling qualified; no production reset or upload authorized or
performed by this change.** The currently deployed artifact runtime is
`fdeb0b1f91f3e740b4df48707edf94329fdc9a8d`; it does not contain the replacement
endpoint in this document. A future rollout needs a newly pinned runtime review,
deployment authorization, and the execution confirmations below.

This workflow deliberately replaces one disposable pilot workspace. It is not a
bucket reset, account reset, migration, scheduler, compatibility layer, or broad
admin API. The gate is disabled unless both an exact workspace UUID and frozen
replacement SHA-256 are configured.

## Frozen replacement contract

The directory passed to `scripts/artifact-replacement.py` contains:

```text
replacement.json
objects/facts/<sha256>.json
objects/summary/<sha256>.json
```

`replacement.json` uses `ct.artifact-replacement.v1`. It requires:

- exactly `since_days: 7` and `privacy_contract:
  ct.published-facts-only.v1`;
- `raw_sources_included: false`;
- one exact workspace and agent, portable project metadata, synthetic source
  identities/checkpoints, and a complete graph inventory;
- exact object count and total bytes, content-addressed filenames, object
  SHA-256/byte references, and a canonical replacement SHA-256;
- canonical `ct.published_facts.v1` and `ct.prepared-summary.v1` objects whose
  graph identities, fact counts, fact digests, row hashes, and references agree.

Validation rejects extra object files, local paths, data URIs, embedded base64
blobs, and populated raw-log/transcript/prompt/blob fields. It does not read the
Mac source logs. Producing the fresh seven-day filtered replacement on the Mac,
transferring it through an approved scoped channel, and freezing its hash are
execution dependencies; current, wider, raw, or synthetic inventory must not be
substituted for the reviewed pilot export.

Offline validation and a non-mutating scope preview need no credential:

```bash
PYTHONPATH=packages/core/src uv run python scripts/artifact-replacement.py \
  validate /secure/path/to/frozen-replacement
PYTHONPATH=packages/core/src uv run python scripts/artifact-replacement.py \
  preview /secure/path/to/frozen-replacement
```

Record the reported workspace, replacement SHA-256, source/graph/object counts,
and bytes. Any mismatch is a stop.

## Exact destructive scope

With `W` as the confirmed workspace, replacement clears:

1. **All SQL in only the Durable Object named `W`** using Durable Object
   `deleteAll()`, then recreates current schemas. The preview reports counts for
   `sequence`, `records`, `resources`, staged and committed fact tables,
   artifact manifests, cleanup state, and artifact upload claims, plus record
   kinds.
2. **Only R2 keys under `workspaces/W/artifacts/`**. Each request deletes at
   most four 1,000-key pages. It always relists from the prefix start, so the
   same exact request safely resumes after interruption or an incomplete batch.

Before deleting R2, the target Durable Object persists the exact workspace and
export identity with `incomplete` status. While incomplete, normal collector
mutations and artifact claims are rejected. Completion is persisted before the
response is returned. Every later execute for that same workspace/export is a
non-destructive success (`already_complete: true`, zero deletion, no SQL reset),
including a retry after a lost completion response or after import. A newly
approved export hash is a distinct destructive replacement and must repeat the
full approval process.

It preserves principal/cursor secrets, deployed bindings and configuration,
the Durable Object namespace/class, all other workspace Durable Objects, and
all other R2 prefixes. It does not reset or reduce Cloudflare daily read/write
quota. Cleanup and collection must be quiescent for the target until the reset
reports `complete: true`; do not import between bounded reset calls.

The preview scans up to 10,000 target objects and reports `truncated`. Execution
must not proceed unless the returned workspace, export hash, SQL scope, R2
prefix, and preservation list match the approved scope and `truncated` is
`false`. A larger prefix needs a separately reviewed preview mechanism, not an
assumption about the unseen remainder.

## Staged execution checklist

These commands are a future runbook, not authorization to run them now.

1. Pin and review the exact runtime. Confirm no source/integration drift and
   preserve the existing `WORKSPACES`, `ARTIFACTS`, `CT_PRINCIPALS`, and
   `CT_CURSOR_KEY` bindings.
2. Confirm a secure owner credential whose principal is scoped to exactly `W`.
   Put it only in a process environment variable; never print or commit it.
3. Configure both temporary exact gates:
   `CT_REPLACEMENT_WORKSPACE_ID=W` and
   `CT_REPLACEMENT_EXPORT_SHA256=H`. Do not alter principal mappings merely to
   make reset work.
4. Validate the frozen bundle again and run one remote preview. Both remote
   commands perform `ct_workspace_snapshot` first as quota/access preflight. If
   it returns `database_read_quota_exceeded`, stop without polling; reset would
   not restore that quota.

   ```bash
   export CT_ACCESS_TOKEN=... # secure process environment only
   PYTHONPATH=packages/core/src uv run python scripts/artifact-replacement.py \
     reset-preview BUNDLE --url ORIGIN \
     --confirmed-workspace W --confirmed-export-sha256 H
   ```

5. Obtain explicit approval for workspace `W`, replacement `H`, and destructive
   scope `workspace-sql-all+workspace-artifact-r2-prefix`. Then invoke one
   bounded reset:

   ```bash
   PYTHONPATH=packages/core/src uv run python scripts/artifact-replacement.py \
     reset-execute BUNDLE --url ORIGIN \
     --confirmed-workspace W --confirmed-export-sha256 H \
     --confirmed-destructive-scope \
       workspace-sql-all+workspace-artifact-r2-prefix
   ```

   There is no automatic retry or busy loop. A missing response has unknown
   outcome: preview/snapshot the exact target before deciding whether to rerun.
   If `complete` is false, inspect the returned count and explicitly repeat the
   same command. Repetition resumes the same prefix without another SQL reset.
   If the successful response is lost, the exact retry returns
   `already_complete: true` without touching replacement data.
6. Require snapshot zero, empty project inventory, and `complete: true`. Disable
   both `CT_REPLACEMENT_*` gates immediately and verify the method is unavailable
   **before import**. This operational teardown is defense in depth; the durable
   completion record already makes an accidentally repeated authorized execute
   non-destructive.
7. Import using only normal authenticated project/source/checkpoint,
   immutable upload, and atomic artifact-publication APIs:

   ```bash
   PYTHONPATH=packages/core/src uv run python scripts/artifact-replacement.py \
     import BUNDLE --url ORIGIN \
     --confirmed-workspace W --confirmed-export-sha256 H
   ```

   An interruption after exact project registration can resume. Checkpoints,
   uploads, and publication are idempotent; a committed publication response
   retry reuses its receipt. Resume is refused unless remote inventory is empty
   or consists of the sole exact frozen project, and complete source inventory
   is still enforced by publication.
8. Run `verify` with the same confirmations. It pins a snapshot, verifies the
   sole project and complete graph/reference metadata, exact object count/bytes,
   and performs only the minimum artifact content reads: facts and summary for
   the first graph.
9. Remove the bearer from the environment. Do not add a scheduler.

## Offline qualification

No unit tests or production calls are added. The disposable qualification uses
actual loopback Wrangler Durable Objects and R2 with two workspaces:

```bash
PYTHONPATH=packages/core/src uv run python \
  scripts/qualify-artifact-replacement.py prepare /tmp/ct-replacement
# Start local Wrangler with the printed digest, synthetic principals, a fresh
# --persist-to directory, and both CT_REPLACEMENT_* variables.
PYTHONPATH=packages/core/src uv run python \
  scripts/qualify-artifact-replacement.py qualify /tmp/ct-replacement \
  --url http://127.0.0.1:8794
```

The qualification proves exact preview counts; role/workspace/hash/confirmation
rejection; target-only SQL and R2 deletion; a 4,000-object bound followed by
explicit 103-object resume; collector rejection during the incomplete state;
lost-completion-response retry; non-destructive execute after import;
unrelated-workspace survival before and after reset and import; privacy
rejection; import continuation after project registration; committed retry
idempotency; exact counts/bytes; and minimum facts/summary reads. All fixtures
are synthetic and remain offline.

Inactive workspaces still receive no scheduled maintenance. Abandoned objects
or claims can remain until a changed publication or an explicitly authorized
replacement. The artifact architecture continues to assume one trusted
publisher per workspace and that the Worker plus serialized cleanup are the only
R2 mutators.
