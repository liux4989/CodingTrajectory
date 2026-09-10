import { bounded, digest, DIGEST, Fault, fields, Json, MAX_ARTIFACT, object, requireThat, stable, State, uuid } from "./shared";
import { safeChronicle } from "./collector";

const MAX_NODE = 64 * 1024;
const MAX_BATCH = 256 * 1024;
const MAX_NODES = 16384;
const MAX_PACK_READ = 32 * 1024 * 1024;
const encoder = new TextEncoder();
const bytes = (value: string) => encoder.encode(value).length;
type Descriptor = { digest: string; agent: string; pack_key: string; offset: number; length: number; pack_bytes: number; validation_version: number };

function normalizedNode(raw: Json): Json {
  requireThat(["json", "object", "array", "concat"].includes(raw.kind), "invalid_chunk_kind");
  const node = raw.kind === "json" ? { kind: raw.kind, fragment: raw.fragment } : { kind: raw.kind, entries: raw.entries };
  if (node.kind === "json") {
    requireThat(typeof node.fragment === "string" && raw.entries == null, "invalid_chunk");
    let value;
    try { value = JSON.parse(node.fragment); } catch { throw new Fault(400, "invalid_chunk_json"); }
    safeChronicle(value);
  } else {
    requireThat(raw.fragment == null, "invalid_chunk");
    const values = node.kind === "object" ? Object.values(object(node.entries)) : node.entries;
    requireThat(Array.isArray(values) && values.length <= 128 && values.every(v => typeof v === "string" && DIGEST.test(v)), "invalid_chunk_references");
    if (node.kind === "object") requireThat(Object.keys(node.entries).every(key => key.length <= 128 && !["__proto__", "constructor", "prototype"].includes(key)), "invalid_chunk_key");
  }
  return node;
}

export async function uploadChunks(state: State, bucket: R2Bucket, request: Json): Promise<Json> {
  requireThat(bytes(stable(request)) <= MAX_BATCH, "chunk_batch_too_large", 413);
  const verified: { digest: string; encoded: Uint8Array; offset: number }[] = [];
  let total = 0;
  for (const entry of request.chunks) {
    const encoded = encoder.encode(stable(normalizedNode(entry.node)));
    requireThat(encoded.length <= MAX_NODE && await digest(encoded) === entry.content_sha256, "chunk_digest_mismatch");
    verified.push({ digest: entry.content_sha256, encoded, offset: total });
    total += encoded.length;
  }
  // One bounded immutable pack per batch avoids an R2 request per tiny item.
  // Logical nodes remain individually addressed. SQLite stores descriptors.
  const pack = new Uint8Array(total);
  for (const entry of verified) pack.set(entry.encoded, entry.offset);
  const key = `${request.workspace_id}/canonical-chunks/v1/${await digest(pack)}`;
  await bucket.put(key, pack, { onlyIf: { etagDoesNotMatch: "*" }, httpMetadata: { contentType: "application/octet-stream" } });
  const stored = await bucket.head(key);
  requireThat(stored?.size === total, "chunk_pack_unavailable", 503);
  // No reader references exist yet. A crash leaves an orphan pack; retry repairs
  // descriptors using identical bytes. Reference-safe deletion stays disabled.
  for (const entry of verified) state.sql.exec(
    "INSERT INTO upload_chunk_objects VALUES(?,?,?,?,?,?,1) ON CONFLICT(digest,agent) DO UPDATE SET pack_key=excluded.pack_key,offset=excluded.offset,length=excluded.length,pack_bytes=excluded.pack_bytes,validation_version=excluded.validation_version",
    entry.digest, request.agent_id, key, entry.offset, entry.encoded.length, total);
  return { stored: verified.map(entry => entry.digest), transferred_bytes: total, validation_version: 1 };
}

function descriptor(state: State, agent: string, digest: string): Descriptor | undefined {
  return state.sql.exec<Descriptor>("SELECT * FROM upload_chunk_objects WHERE digest=? AND agent=?", digest, agent).toArray()[0];
}

export async function missingChunks(state: State, bucket: R2Bucket, request: Json): Promise<Json> {
  requireThat(request.digests.every((v: unknown) => typeof v === "string" && DIGEST.test(v)), "invalid_chunk_digest");
  const packs = new Map<string, number | null>();
  const missing: string[] = [];
  for (const id of request.digests) {
    const row = descriptor(state, request.agent_id, id);
    if (!row) { missing.push(id); continue; }
    if (!packs.has(row.pack_key)) packs.set(row.pack_key, (await bucket.head(row.pack_key))?.size ?? null);
    if (packs.get(row.pack_key) !== row.pack_bytes) missing.push(id);
  }
  return { missing };
}

function nodeReader(state: State, bucket: R2Bucket, agent: string) {
  const packs = new Map<string, Uint8Array>();
  const nodes = new Map<string, Json>();
  let packBytes = 0;
  return async (id: string): Promise<Json> => {
    if (nodes.has(id)) return nodes.get(id)!;
    const row = descriptor(state, agent, id);
    requireThat(row, "missing_chunk", 409);
    requireThat(row.validation_version === 1 && row.length <= MAX_NODE && row.pack_bytes <= MAX_BATCH && row.offset >= 0 && row.length > 0 && row.offset + row.length <= row.pack_bytes, "invalid_chunk_descriptor", 503);
    if (!packs.has(row.pack_key)) {
      packBytes += row.pack_bytes;
      requireThat(packBytes <= MAX_PACK_READ, "chunk_pack_read_budget", 413);
      const stored = await bucket.get(row.pack_key);
      requireThat(stored, "chunk_unavailable", 503);
      const pack = await bounded(stored.body, MAX_BATCH);
      requireThat(pack.length === row.pack_bytes, "corrupt_chunk_pack", 503);
      packs.set(row.pack_key, pack);
    }
    const raw = packs.get(row.pack_key)!.subarray(row.offset, row.offset + row.length);
    requireThat(await digest(raw) === id, "corrupt_chunk", 503);
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    nodes.set(id, value);
    return value;
  };
}

export async function reconstruct(state: State, bucket: R2Bucket, agent: string, root: string): Promise<{ canonical: string; digests: string[] }> {
  const getNode = nodeReader(state, bucket, agent);
  const visited = new Set<string>();
  const ancestors = new Set<string>();
  let expandedBytes = 0;
  let visits = 0;
  async function read(id: string, depth = 0): Promise<string> {
    requireThat(!ancestors.has(id), "chunk_cycle");
    requireThat(depth <= 32 && ++visits <= MAX_NODES, "chunk_graph_too_large", 413);
    ancestors.add(id);
    const node = await getNode(id);
    visited.add(id);
    let result: string;
    if (node.kind === "json") {
      expandedBytes += bytes(node.fragment);
      requireThat(expandedBytes <= MAX_ARTIFACT, "artifact_too_large", 413);
      result = node.fragment;
    } else if (node.kind === "object") {
      const entries: string[] = [];
      for (const key of Object.keys(node.entries).sort()) entries.push(`${JSON.stringify(key)}:${await read(node.entries[key], depth + 1)}`);
      result = `{${entries.join(",")}}`;
    } else {
      const entries: string[] = [];
      for (const entry of node.entries) {
        const child = await read(entry, depth + 1);
        if (node.kind === "concat") {
          requireThat(child.startsWith("[") && child.endsWith("]"), "invalid_concat_child");
          if (child.length > 2) entries.push(child.slice(1, -1));
        } else entries.push(child);
      }
      result = `[${entries.join(",")}]`;
    }
    requireThat(bytes(result) <= MAX_ARTIFACT, "artifact_too_large", 413);
    ancestors.delete(id);
    return result;
  }
  return { canonical: await read(root), digests: [...visited] };
}

export async function uploadRead(state: State, bucket: R2Bucket, method: string, request: Json): Promise<Json> {
  fields(request, ["workspace_id", "project_id", "snapshot_sequence", "artifact_id", "digests"], ["workspace_id"]);
  const sequence = state.pin(request.snapshot_sequence);
  if (request.project_id != null) uuid(request.project_id);
  if (method === "ct_publication_watermark") {
    // Indexed sequence scan, not State.all(). Legacy artifacts supply the initial
    // migration watermark; staging, leases, and estimator jobs never contribute.
    const row = state.sql.exec<{ watermark: number }>("SELECT COALESCE(MAX(CAST(json_extract(payload,'$.published_sequence') AS INTEGER)),0) AS watermark FROM records WHERE kind IN ('artifact','publication_watermark') AND sequence<=? AND (? IS NULL OR json_extract(payload,'$.project_id')=?)", sequence, request.project_id ?? null, request.project_id ?? null).one();
    return { workspace_id: request.workspace_id, project_id: request.project_id ?? null, published_sequence: row.watermark };
  }
  const row = state.get("artifact", uuid(request.artifact_id), sequence);
  requireThat(row && !row.deleted && (!request.project_id || row.project_id === request.project_id), "artifact_not_found", 404);
  const root = row.chunk_root_sha256 ?? null;
  if (method === "ct_artifact_chunk_manifest") return { workspace_id: request.workspace_id, snapshot_sequence: sequence,
    artifact_id: row.artifact_id, revision: row.revision, published_sequence: row.published_sequence,
    schema_version: row.schema_version, content_sha256: row.content_sha256, root_sha256: root,
    agent_id: row.agent_id, source_ids: row.source_ids, uncompressed_bytes: row.uncompressed_bytes,
    transport: root ? "ct.canonical_chunks.v1" : "gzip" };
  requireThat(root, "legacy_artifact_requires_gzip", 409);
  requireThat(Array.isArray(request.digests) && request.digests.length > 0 && request.digests.length <= 128, "invalid_chunk_selection");
  // Authorize the entire selection before any object-store I/O.
  for (const id of request.digests) {
    requireThat(typeof id === "string" && DIGEST.test(id), "invalid_chunk_digest");
    requireThat(state.sql.exec("SELECT 1 FROM upload_members WHERE root=? AND digest=? AND agent=?", root, id, row.agent_id).toArray().length, "chunk_not_in_revision", 403);
  }
  const getNode = nodeReader(state, bucket, row.agent_id);
  const chunks: Json[] = [];
  let total = 0;
  for (const id of request.digests) {
    const node = await getNode(id);
    total += bytes(stable(node)) + 100;
    requireThat(total <= MAX_BATCH - 1024, "narrow_chunk_selection", 413);
    chunks.push({ content_sha256: id, node });
  }
  return { workspace_id: request.workspace_id, snapshot_sequence: sequence, artifact_id: row.artifact_id, revision: row.revision, chunks };
}

export function initializeUploads(state: State) {
  state.sql.exec(`CREATE TABLE IF NOT EXISTS upload_chunk_objects (digest TEXT NOT NULL, agent TEXT NOT NULL, pack_key TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, pack_bytes INTEGER NOT NULL, validation_version INTEGER NOT NULL, PRIMARY KEY(digest,agent));
    CREATE TABLE IF NOT EXISTS upload_members (root TEXT NOT NULL, digest TEXT NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(root,digest,agent));
    CREATE TABLE IF NOT EXISTS upload_authority (id INTEGER PRIMARY KEY CHECK(id=1), incarnation TEXT NOT NULL);`);
  state.sql.exec("INSERT OR IGNORE INTO upload_authority VALUES(1,?)", crypto.randomUUID());
}
