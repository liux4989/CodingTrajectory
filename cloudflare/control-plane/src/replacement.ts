import { Json, requireThat, State } from "./shared";

const RESET_R2_PAGES = 4;
const PREVIEW_R2_PAGES = 10;
const TABLES = [
  "sequence", "records", "resources", "staged_fact_rows", "fact_rows",
  "fact_schema", "staged_fact_items", "staged_fact_generations",
  "validated_fact_graphs", "artifact_manifests", "artifact_cleanup",
  "artifact_upload_claims",
] as const;

export function replacementPrefix(workspaceId: string): string {
  return `workspaces/${workspaceId}/artifacts/`;
}

/** Describe the exact workspace-local SQL and R2 scope without mutating it. */
export async function previewWorkspaceReplacement(
  state: State, env: Env, workspaceId: string, exportSha256: string,
): Promise<Json> {
  const tables: Record<string, number> = {};
  for (const table of TABLES) {
    tables[table] = state.sql.exec<{ count: number }>(
      `SELECT COUNT(*) AS count FROM ${table}`,
    ).one().count;
  }
  const recordKinds = Object.fromEntries(state.sql.exec<{ kind: string; count: number }>(
    "SELECT kind,COUNT(*) AS count FROM records GROUP BY kind ORDER BY kind",
  ).toArray().map(row => [row.kind, row.count]));
  const objects = await inspectPrefix(env.ARTIFACTS, replacementPrefix(workspaceId), PREVIEW_R2_PAGES);
  return {
    mode: "preview", workspace_id: workspaceId, expected_export_sha256: exportSha256,
    sql: { delete_all: true, tables, record_kinds: recordKinds },
    r2: objects,
    preserved: ["CT_PRINCIPALS", "CT_CURSOR_KEY", "WORKSPACES binding",
      "ARTIFACTS binding", "other workspace Durable Objects", "other R2 prefixes"],
    quota: "reset does not restore Cloudflare daily counters",
  };
}

/** Delete only one workspace's R2 prefix in bounded, restartable batches. */
export async function deleteWorkspaceArtifactPrefix(
  env: Env, workspaceId: string,
): Promise<{ prefix: string; deleted: number; complete: boolean }> {
  const prefix = replacementPrefix(workspaceId);
  let deleted = 0;
  for (let page = 0; page < RESET_R2_PAGES; page++) {
    // Always restart at the prefix beginning. Deleting a listed page can
    // invalidate a continuation cursor, while this converges safely on retry.
    const objects = await env.ARTIFACTS.list({ prefix, limit: 1000 });
    if (!objects.objects.length) return { prefix, deleted, complete: true };
    const keys = objects.objects.map(object => object.key);
    requireThat(keys.every(key => key.startsWith(prefix)), "replacement_prefix_escape", 500);
    await env.ARTIFACTS.delete(keys);
    deleted += keys.length;
  }
  const remaining = await env.ARTIFACTS.list({ prefix, limit: 1 });
  return { prefix, deleted, complete: remaining.objects.length === 0 };
}

async function inspectPrefix(bucket: R2Bucket, prefix: string, maxPages: number): Promise<Json> {
  let cursor: string | undefined;
  let count = 0;
  let bytes = 0;
  for (let page = 0; page < maxPages; page++) {
    const result = await bucket.list({ prefix, cursor, limit: 1000 });
    count += result.objects.length;
    bytes += result.objects.reduce((total, object) => total + object.size, 0);
    if (!result.truncated || !result.cursor) {
      return { prefix, objects: count, bytes, truncated: false };
    }
    cursor = result.cursor;
  }
  return { prefix, objects: count, bytes, truncated: true };
}
