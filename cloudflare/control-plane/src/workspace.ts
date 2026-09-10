import { DurableObject } from "cloudflare:workers";
import { bounded, decode, digest, encode, Fault, Json, MAX_ARTIFACT, Principal, requireThat, stable, State, validate } from "./shared";
import { checkpoint, chronicleMetadata, publication, recovery, registerProject, registerSource } from "./collector";
import { livingRead, livingWrite } from "./living";
import { estimation } from "./estimation";

/** A workspace is the authorization, transaction and revision boundary. */
export class Workspace extends DurableObject<Env> {
  private state: State;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.state = new State(ctx.storage.sql);
  }

  async invoke(method: string, envelopeJson: string, principalJson: string): Promise<string> {
    const envelope = JSON.parse(envelopeJson);
    const principal = JSON.parse(principalJson);
    return JSON.stringify(await this.rpc(method, envelope, principal));
  }

  async rpc(method: string, envelope: Json, principal: Principal): Promise<{ status: number; body: Json }> {
    try {
      const request = envelope.request;
      requireThat(request.workspace_id === principal.workspace_id, "workspace_denied", 403);
      // Compute identity before schema defaults normalize the request.
      const identity = await digest(stable(request));
      validate(method, request);
      if (method === "ct_collector_publish_observation") {
        requireThat(request.payload.source_checkpoint?.segments?.every((offset: unknown) => Number.isSafeInteger(offset) && Number(offset) > 0), "invalid_checkpoint_offsets");
        requireThat(await digest(stable(request.payload)) === request.content_sha256 && request.event_id === `checkpoint:${request.content_sha256}`, "checkpoint_digest_mismatch");
      }
      if (method === "ct_collector_stage_artifact_payload") return { status: 200, body: await this.stage(request) };
      if (["ct_historical_snapshot", "ct_historical_artifacts", "ct_project_sessions_projection"].includes(method)) {
        return { status: 200, body: await this.historical(method, request) };
      }
      const body = this.ctx.storage.transactionSync(() => {
        const key = envelope.idempotency_key ? stable([principal.agent_id, method, envelope.idempotency_key]) : null;
        const prior = key ? this.state.get("receipt", key) : undefined;
        if (prior) {
          requireThat(prior.identity === identity, "idempotency_conflict", 409);
          return prior.result;
        }
        const result = this.dispatch(method, request, principal);
        if (key) this.state.put("receipt", key, { identity, result }, this.state.head());
        return result;
      });
      return { status: 200, body };
    } catch (error) {
      return { status: error instanceof Fault ? error.status : 503,
        body: { error: { code: error instanceof Fault ? error.code : "authority_unavailable" } } };
    }
  }

  private dispatch(method: string, request: Json, principal: Principal): Json {
    switch (method) {
      case "ct_project_register": return registerProject(this.state, request);
      case "ct_collector_register_source": return registerSource(this.state, request);
      case "ct_collector_recover": return recovery(this.state, request);
      case "ct_collector_publish_observation": validate("checkpoint", request.payload); return checkpoint(this.state, request);
      case "ct_collector_publish_artifacts": return publication(this.state, request);
      case "ct_collector_heartbeat": case "ct_collector_publish_living_observation": return livingWrite(this.state, method, request);
      case "ct_remote_living": return livingRead(this.state, request);
      case "ct_workspace_snapshot": return { workspace_id: request.workspace_id, snapshot_sequence: this.state.pin(request.snapshot_sequence) };
      case "ct_project_inventory_snapshot": {
        const sequence = this.state.pin(request.snapshot_sequence);
        const artifacts = this.state.all("artifact", sequence).filter(row => !row.deleted);
        const projects = this.state.all("project", sequence).filter(row => !request.modified_since || Date.parse(row.modified_at) >= Date.parse(request.modified_since)).map((row): Json => ({
          ...row, vendors: [...new Set(artifacts.filter(a => a.project_id === row.project_id).flatMap(a => a.vendors))].sort(),
        })).filter(row => !request.agent_vendor || row.vendors.includes(request.agent_vendor)).sort((a, b) => a.display_name.localeCompare(b.display_name));
        return { workspace_id: request.workspace_id, snapshot_sequence: sequence, projects };
      }
      default: return estimation(this.state, method, request, principal);
    }
  }

  private async stage(request: Json): Promise<Json> {
    const compressed = decode(request.payload_base64);
    requireThat(compressed.length === request.compressed_bytes, "compressed_size_mismatch");
    const canonical = await bounded(new Blob([compressed as Uint8Array<ArrayBuffer>]).stream().pipeThrough(new DecompressionStream("gzip")), MAX_ARTIFACT);
    requireThat(canonical.length === request.uncompressed_bytes && await digest(canonical) === request.content_sha256, "artifact_digest_mismatch");
    let payload;
    try { payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(canonical)); }
    catch { throw new Fault(400, "invalid_artifact_json"); }
    const { metadata, resources } = chronicleMetadata(payload);
    const variants = ["default", "runtime", "usage", "runtime_usage"];
    requireThat(stable(Object.keys(request.projections).sort()) === stable([...variants].sort()), "invalid_projection_variants");
    for (const variant of variants) validate("project_sessions_response", request.projections[variant]);
    const projectionIdentity = await digest(stable(request.projections));
    const key = `${request.workspace_id}/${request.content_sha256}.gzip`;
    // R2 is written before the SQLite manifest. Unreferenced objects are harmless;
    // readers can only access objects referenced by an authorized committed revision.
    await this.env.ARTIFACTS.put(key, compressed, { onlyIf: { etagDoesNotMatch: "*" }, httpMetadata: { contentType: "application/gzip" } });
    return this.ctx.storage.transactionSync(() => {
      const existingProjections = this.state.get("projections", request.content_sha256);
      requireThat(!existingProjections || stable(existingProjections) === stable(request.projections), "projection_conflict", 409);
      const stageKey = `${request.agent_id}:${request.content_sha256}`;
      const prior = this.state.get("staged", stageKey);
      requireThat(!prior || prior.projection_identity === projectionIdentity, "projection_conflict", 409);
      if (!prior) {
        const sequence = this.state.next();
        this.state.put("staged", stageKey, { ...metadata, key, content_sha256: request.content_sha256,
          compressed_bytes: compressed.length, uncompressed_bytes: canonical.length, projection_identity: projectionIdentity }, sequence);
        this.state.put("projections", request.content_sha256, request.projections, sequence);
        for (const resource of resources) this.state.sql.exec("INSERT OR IGNORE INTO resources VALUES(?,?)", request.content_sha256, resource);
      }
      return { content_sha256: request.content_sha256, encoding: "gzip", uncompressed_bytes: canonical.length, compressed_bytes: compressed.length, projection_version: 1 };
    });
  }

  private async historical(method: string, request: Json): Promise<Json> {
    // No await until every descriptor and projection is selected at the same S.
    const sequence = this.state.pin(request.snapshot_sequence);
    const base = { workspace_id: request.workspace_id, snapshot_sequence: sequence };
    if (request.metadata_only) return { ...base, artifacts: [] };
    const projects = new Map(this.state.all("project", sequence).map(p => [p.project_id, p]));
    const resources = request.resource_ids ?? [];
    requireThat(Array.isArray(resources) && resources.length <= 1000, "invalid_resource_ids");
    const since = request.since_days == null ? null : Date.now() - request.since_days * 86400000;
    const rows = this.state.all("artifact", sequence).filter(row => !row.deleted
      && (!request.project_name || projects.get(row.project_id)?.display_name === request.project_name)
      && (!request.agent_vendor || row.vendors.includes(request.agent_vendor))
      && (since == null || Date.parse(row.observed_at) >= since)
      && (!request.modified_since || Date.parse(row.observed_at) >= Date.parse(request.modified_since))
      && (!resources.length || resources.some(id => this.state.sql.exec("SELECT 1 FROM resources WHERE digest=? AND resource_id=?", row.content_sha256, id).toArray().length))
    ).sort((a, b) => a.artifact_id.localeCompare(b.artifact_id));
    if (method === "ct_project_sessions_projection") {
      const include = request.include ?? [];
      requireThat(Array.isArray(include), "invalid_include");
      const variant = include.includes("runtime") ? include.includes("usage") ? "runtime_usage" : "runtime" : include.includes("usage") ? "usage" : "default";
      let complete = true;
      const items = rows.flatMap(row => {
        const projection = this.state.get("projections", row.content_sha256, sequence)?.[variant];
        if (!projection) { complete = false; return []; }
        return projection.items.filter((item: Json) => !request.agent_vendor || item.vendors.includes(request.agent_vendor));
      }).sort((a, b) => (a.project ?? "").localeCompare(b.project ?? ""));
      return { ...base, complete, result: { items } };
    }
    requireThat(rows.reduce((total, row) => total + row.compressed_bytes, 0) <= 32 * 1024 * 1024, "narrow_historical_scope", 413);
    const artifacts: Json[] = [];
    for (const row of rows) {
      const stored = await this.env.ARTIFACTS.get(row.key);
      requireThat(stored, "artifact_unavailable", 503);
      artifacts.push({ artifact_id: row.artifact_id, revision: row.revision, published_sequence: row.published_sequence,
        content_sha256: row.content_sha256, payload_encoding: "gzip", uncompressed_bytes: row.uncompressed_bytes,
        payload_base64: encode(await bounded(stored.body, MAX_ARTIFACT)) });
    }
    return { ...base, artifacts };
  }
}
