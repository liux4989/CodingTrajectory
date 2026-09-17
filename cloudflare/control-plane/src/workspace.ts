import { DurableObject } from "cloudflare:workers";
import { authorityFailure, digest, Fault, Json, Principal, requireThat, stable, State, validate } from "./shared";
import { artifactManifests, artifactReadLocator, cleanupArtifactObjects, commitArtifactPublication, initializeArtifacts, prepareArtifactPublication, pruneArtifactReceipts } from "./artifacts";
import { checkpoint, recovery, registerProject, registerSource } from "./collector";
import { commitPublication, factRead, initializeFacts, missingFactRows, preparePublication, verifyStageRows, writeStagedRows } from "./facts";
import { livingRead, livingWrite } from "./living";

/** A workspace is the authorization, transaction and revision boundary. */
export class Workspace extends DurableObject<Env> {
  private state: State;
  private cursorSecret: string;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.state = new State(ctx.storage.sql);
    this.cursorSecret = env.CT_CURSOR_KEY;
    this.ctx.storage.transactionSync(() => {
      initializeFacts(this.state);
      initializeArtifacts(this.state);
    });
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
      if (method === "ct_fact_read") {
        return { status: 200, body: await factRead(this.state, request, this.cursorSecret) };
      }
      if (method === "ct_artifact_read") {
        return { status: 200, body: artifactReadLocator(this.state, request) };
      }
      if (method === "ct_collector_missing_fact_rows") return { status: 200, body: missingFactRows(this.state, request) };
      if (method === "ct_collector_stage_fact_rows") {
        await verifyStageRows(request);
        return { status: 200, body: this.ctx.storage.transactionSync(() => writeStagedRows(this.state, request)) };
      }
      if (method === "ct_collector_publish_facts") {
        const key = envelope.idempotency_key
          ? stable([principal.agent_id, method, envelope.idempotency_key])
          : null;
        const prior = key ? this.state.get("receipt", key) : undefined;
        if (prior) {
          requireThat(prior.identity === identity, "idempotency_conflict", 409);
          return { status: 200, body: prior.result };
        }
        // Hash verification is async (crypto.subtle); fencing and the atomic
        // commit and receipt write run inside one workspace transaction.
        const plan = await preparePublication(this.state, request);
        const body = this.ctx.storage.transactionSync(() => {
          const concurrent = key ? this.state.get("receipt", key) : undefined;
          if (concurrent) {
            requireThat(concurrent.identity === identity, "idempotency_conflict", 409);
            return concurrent.result;
          }
          const result = commitPublication(this.state, request, plan);
          if (key) this.state.put("receipt", key, { identity, result }, this.state.head());
          return result;
        });
        return { status: 200, body };
      }
      if (method === "ct_collector_publish_artifacts") {
        const key = envelope.idempotency_key
          ? stable([principal.agent_id, method, envelope.idempotency_key])
          : null;
        const prior = key ? this.state.get("receipt", key) : undefined;
        if (prior) {
          requireThat(prior.identity === identity, "idempotency_conflict", 409);
          return { status: 200, body: prior.result };
        }
        const plan = await prepareArtifactPublication(this.state, this.env, request);
        const body = this.ctx.storage.transactionSync(() => {
          const concurrent = key ? this.state.get("receipt", key) : undefined;
          if (concurrent) {
            requireThat(concurrent.identity === identity, "idempotency_conflict", 409);
            return concurrent.result;
          }
          const result = commitArtifactPublication(this.state, request, plan);
          if (key) this.state.put("receipt", key, { identity, result }, this.state.head());
          pruneArtifactReceipts(this.state);
          return result;
        });
        this.ctx.waitUntil(cleanupArtifactObjects(this.state, this.env, request.workspace_id)
          .catch(() => console.error(JSON.stringify({ event: "artifact_cleanup_deferred" }))));
        return { status: 200, body };
      }
      if (method === "ct_collector_publish_observation") {
        requireThat(request.payload.source_checkpoint?.segments?.every((offset: unknown) => Number.isSafeInteger(offset) && Number(offset) > 0), "invalid_checkpoint_offsets");
        requireThat(await digest(stable(request.payload)) === request.content_sha256 && request.event_id === `checkpoint:${request.content_sha256}`, "checkpoint_digest_mismatch");
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
      const failure = authorityFailure(error, "workspace");
      return { status: failure.status, body: { error: { code: failure.code } } };
    }
  }

  private dispatch(method: string, request: Json, principal: Principal): Json {
    switch (method) {
      case "ct_project_register": return registerProject(this.state, request);
      case "ct_collector_register_source": return registerSource(this.state, request);
      case "ct_collector_recover": return recovery(this.state, request);
      case "ct_collector_publish_observation": validate("checkpoint", request.payload); return checkpoint(this.state, request);
      case "ct_collector_heartbeat": case "ct_collector_publish_living_observation": return livingWrite(this.state, method, request);
      case "ct_remote_living": return livingRead(this.state, request);
      case "ct_workspace_snapshot": return { workspace_id: request.workspace_id, snapshot_sequence: this.state.pin(request.snapshot_sequence) };
      case "ct_artifact_manifest": return artifactManifests(this.state, request);
      case "ct_project_inventory_snapshot": {
        const sequence = this.state.pin(request.snapshot_sequence);
        const artifactRows = this.state.sql.exec<{ project_id: string; manifest: string }>(`SELECT project_id,manifest
          FROM artifact_manifests current WHERE workspace_sequence=(
            SELECT MAX(candidate.workspace_sequence) FROM artifact_manifests candidate
            WHERE candidate.project_id=current.project_id AND candidate.workspace_sequence<=?)`, sequence).toArray();
        const expectedArtifactProjects = this.state.all("artifact_project_publisher", sequence)
          .filter(row => row.first_sequence <= sequence);
        if (artifactRows.length !== expectedArtifactProjects.length) {
          throw new Fault(410, "artifact_snapshot_expired");
        }
        const artifactProjects = new Set(artifactRows.map(row => row.project_id));
        const legacyGraphs = this.state.all("graph_publication", sequence)
          .filter(row => !row.deleted && !artifactProjects.has(row.project_id));
        const artifactGraphs = artifactRows.flatMap(row => JSON.parse(row.manifest).graphs.map((graph: Json) => ({
            project_id: row.project_id, vendors: graph.vendors,
          })));
        const graphs = [...legacyGraphs, ...artifactGraphs];
        const projects = this.state.all("project", sequence)
          .filter(row => !request.modified_since || Date.parse(row.modified_at) >= Date.parse(request.modified_since))
          .map((row): Json => ({
            ...row,
            vendors: [...new Set(graphs.filter(graph => graph.project_id === row.project_id).flatMap(graph => graph.vendors))].sort(),
          }))
          .filter(row => !request.agent_vendor || row.vendors.includes(request.agent_vendor))
          .sort((a, b) => a.display_name.localeCompare(b.display_name));
        return { workspace_id: request.workspace_id, snapshot_sequence: sequence, projects };
      }
      default: throw new Fault(404, "not_found");
    }
  }
}
