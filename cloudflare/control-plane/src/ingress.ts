import { bounded, decode, digest, Fault, Json, MAX_ARTIFACT, requireThat, stable, validate } from "./shared";
import { chronicleMetadata } from "./collector";
import { validateReadProjections } from "./catalog";
import { nodeReader, reconstruct } from "./upload";
import { validateResourceProjections } from "./resource-projections";
import type { Workspace } from "./workspace";

/** Bounded compatibility ingestion outside the serial workspace coordinator. */
export async function stageArtifact(workspace: DurableObjectStub<Workspace>, bucket: R2Bucket, method: string, request: Json): Promise<Json> {
  validate(method, request);
  let canonical: Uint8Array<ArrayBuffer>;
  let compressed: Uint8Array<ArrayBuffer>;
  let digests: string[] = [];
  if (method === "ct_collector_stage_chunk_manifest") {
    const result = await reconstruct(nodeReader(bucket, ids => workspace.chunkDescriptors(request.agent_id, ids)), request.root_sha256);
    canonical = new TextEncoder().encode(result.canonical);
    digests = result.digests;
    compressed = new Uint8Array(await bounded(new Blob([canonical]).stream().pipeThrough(new CompressionStream("gzip")), MAX_ARTIFACT));
  } else {
    compressed = decode(request.payload_base64) as Uint8Array<ArrayBuffer>;
    requireThat(compressed.length === request.compressed_bytes, "compressed_size_mismatch");
    canonical = new Uint8Array(await bounded(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")), MAX_ARTIFACT));
  }
  requireThat(canonical.length === request.uncompressed_bytes && await digest(canonical) === request.content_sha256, "artifact_digest_mismatch");
  let payload;
  try { payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(canonical)); }
  catch { throw new Fault(400, "invalid_artifact_json"); }
  const { metadata, resources } = chronicleMetadata(payload);
  const variants = ["default", "runtime", "usage", "runtime_usage"];
  requireThat(stable(Object.keys(request.projections).filter(key => !["canonical","resources"].includes(key)).sort()) === stable([...variants].sort()), "invalid_projection_variants");
  for (const variant of variants) validate("project_sessions_response", request.projections[variant]);
  if (request.projections.canonical != null) {
    validateReadProjections(request.projections.canonical);
    requireThat(request.projections.canonical.content_sha256 === request.content_sha256 && request.projections.canonical.root_session_id === metadata.artifact_id, "read_projection_identity");
  }
  const independent = request.projections.resources;
  if (independent) validateResourceProjections(independent, metadata, resources, payload);
  // V1 identities predate page fields; schema defaults must not change retry identity.
  const identityPayload = independent?.schema_version === "ct.resource_projections.v1"
    ? {...independent, rows: independent.rows.map(({page_index, page_count, ...row}: Json) => row)} : independent;
  const resourceIdentity = independent ? await digest(stable([request.content_sha256, identityPayload])) : null;
  // Payloads are stored once in independent rows, not copied into the old record.
  const { resources: _resources, ...projections } = request.projections;
  const key = `${request.workspace_id}/${request.content_sha256}.gzip`;
  await bucket.put(key, compressed, { onlyIf: { etagDoesNotMatch: "*" }, httpMetadata: { contentType: "application/gzip" } });
  // A failed/retried page may leave invisible indexes, never a ready descriptor.
  // Finalization checks complete coverage before publication can select a stage.
  for (let offset = 0; offset < Math.max(resources.length, digests.length); offset += 128) {
    await workspace.indexStage(JSON.stringify({ agent_id: request.agent_id, content_sha256: request.content_sha256,
      root_sha256: request.root_sha256, resources: resources.slice(offset, offset + 128), digests: digests.slice(offset, offset + 128) }));
  }
  let page: Json[] = [], bytes = 0;
  const flush = async () => {
    if (!page.length) return;
    await workspace.indexResourceProjections(JSON.stringify({identity: resourceIdentity, rows: page}));
    page = []; bytes = 0;
  };
  for (const row of independent?.rows ?? []) {
    const size = new TextEncoder().encode(stable(row)).length;
    if (page.length >= 128 || bytes + size > 256*1024) await flush();
    page.push(row); bytes += size;
  }
  await flush();
  const result = JSON.parse(await workspace.completeStage(JSON.stringify({ agent_id: request.agent_id, content_sha256: request.content_sha256,
    root_sha256: request.root_sha256, metadata, key, uncompressed_bytes: canonical.length, compressed_bytes: compressed.length,
    projections, projection_identity: await digest(stable(projections)),
    resource_projection_identity: resourceIdentity, resource_projection_count: independent?.rows.length ?? 0,
    resource_projection_version: independent?.schema_version === "ct.resource_projections.v2" ? 2 : 1,
    resource_count: resources.length, chunk_count: digests.length })));
  if (result.status !== 200) throw new Fault(result.status, result.body.error.code);
  return result.body;
}
