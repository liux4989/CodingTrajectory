import { DurableObject } from "cloudflare:workers";
import { authorityFailure, digest, Fault, Json, Principal, requireThat, stable, State, validate } from "./shared";
import { artifactManifests, artifactReadLocator, claimArtifactUpload, cleanupArtifactObjects, commitArtifactPublication, initializeArtifacts, prepareArtifactPublication, pruneArtifactReceipts } from "./artifacts";
import { checkpoint, recovery, registerProject, registerSource } from "./collector";
import { dropEmptyLegacyFactTables, legacyFactTables } from "./legacy-cleanup";
import { livingRead, livingWrite } from "./living";
import { deleteWorkspaceArtifactPrefix, initializeReplacement, markWorkspaceReplacement, previewWorkspaceReplacement, workspaceReplacement } from "./replacement";

const REPLACEMENT_MUTATIONS = new Set([
  "ct_project_register", "ct_collector_register_source", "ct_collector_publish_observation",
  "ct_collector_publish_artifacts", "ct_collector_heartbeat", "ct_collector_publish_living_observation",
  "ct_internal_artifact_claim",
]);

/** A workspace is the authorization, transaction and revision boundary. */
export class Workspace extends DurableObject<Env> {
  private state: State;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.state = new State(ctx.storage.sql);
    this.initialize();
  }

  private initialize() {
    this.ctx.storage.transactionSync(() => {
      initializeArtifacts(this.state);
      initializeReplacement(this.state);
      const target = this.env.CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID;
      if (target && this.ctx.id.toString() === this.env.WORKSPACES.idFromName(target).toString()) {
        dropEmptyLegacyFactTables(this.state);
      }
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
      if (method === "ct_workspace_replace") {
        requireThat(principal.roles.includes("owner"), "capability_required", 403);
        requireThat(this.env.CT_REPLACEMENT_WORKSPACE_ID
          && request.workspace_id === this.env.CT_REPLACEMENT_WORKSPACE_ID,
        "workspace_replacement_target_denied", 403);
        requireThat(this.env.CT_REPLACEMENT_EXPORT_SHA256
          && request.expected_export_sha256 === this.env.CT_REPLACEMENT_EXPORT_SHA256,
        "workspace_replacement_export_denied", 403);
        requireThat(request.confirmation
          === `${request.mode}:${request.workspace_id}:${request.expected_export_sha256}`,
        "workspace_replacement_confirmation_required", 403);
        const expectedObject = this.env.WORKSPACES.idFromName(request.workspace_id).toString();
        requireThat(this.ctx.id.toString() === expectedObject, "workspace_replacement_target_denied", 403);
        if (request.mode === "preview") {
          return { status: 200, body: await previewWorkspaceReplacement(
            this.state, this.env, request.workspace_id, request.expected_export_sha256,
          ) };
        }
        requireThat(request.mode === "execute", "workspace_replacement_mode_invalid");
        return this.ctx.blockConcurrencyWhile(async () => {
          const prior = workspaceReplacement(this.state);
          const same = prior != null && prior.workspace_id === request.workspace_id
            && prior.export_sha256 === request.expected_export_sha256;
          if (prior && same && prior.status === "complete") {
            return { status: 200, body: {
              workspace_id: request.workspace_id, sql_reset: false,
              prefix: `workspaces/${request.workspace_id}/artifacts/`,
              deleted: 0, complete: true, already_complete: true,
            } };
          }
          const resetSql = !same;
          if (!same) {
            await this.ctx.storage.deleteAll();
            this.state = new State(this.ctx.storage.sql);
            this.initialize();
            markWorkspaceReplacement(this.state, request.workspace_id,
              request.expected_export_sha256, "incomplete");
          }
          const deletion = await deleteWorkspaceArtifactPrefix(this.env, request.workspace_id);
          markWorkspaceReplacement(this.state, request.workspace_id,
            request.expected_export_sha256, deletion.complete ? "complete" : "incomplete");
          return { status: 200, body: {
            workspace_id: request.workspace_id, sql_reset: resetSql,
            ...deletion, already_complete: false,
          } };
        });
      }
      const replacement = workspaceReplacement(this.state);
      requireThat(replacement?.status !== "incomplete" || !REPLACEMENT_MUTATIONS.has(method),
        "workspace_replacement_incomplete", 409);
      if (method === "ct_internal_artifact_claim") {
        requireThat(principal.roles.includes("collect") || principal.roles.includes("owner"), "capability_required", 403);
        requireThat(["facts", "summary"].includes(request.kind)
          && typeof request.sha256 === "string"
          && /^[0-9a-f]{64}$/.test(request.sha256), "invalid_artifact_claim");
        this.ctx.storage.transactionSync(() =>
          claimArtifactUpload(this.state, request.kind, request.sha256));
        return { status: 200, body: {} };
      }
      // Compute identity before schema defaults normalize the request.
      const identity = await digest(stable(request));
      validate(method, request);
      if (method === "ct_artifact_read") {
        return { status: 200, body: artifactReadLocator(this.state, request) };
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
        this.ctx.waitUntil(this.ctx.blockConcurrencyWhile(() =>
          cleanupArtifactObjects(this.state, this.env, request.workspace_id)
            .catch(() => console.error(JSON.stringify({ event: "artifact_cleanup_deferred" })))));
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
      case "ct_legacy_fact_cleanup_status": return {
        workspace_id: request.workspace_id, snapshot_sequence: this.state.head(),
        ...legacyFactTables(this.state),
      };
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
        const artifactGraphs = artifactRows.flatMap(row => JSON.parse(row.manifest).graphs.map((graph: Json) => ({
            project_id: row.project_id, vendors: graph.vendors,
          })));
        const projects = this.state.all("project", sequence)
          .filter(row => !request.modified_since || Date.parse(row.modified_at) >= Date.parse(request.modified_since))
          .map((row): Json => ({
            ...row,
            vendors: [...new Set(artifactGraphs.filter(graph => graph.project_id === row.project_id).flatMap(graph => graph.vendors))].sort(),
          }))
          .filter(row => !request.agent_vendor || row.vendors.includes(request.agent_vendor))
          .sort((a, b) => a.display_name.localeCompare(b.display_name));
        return { workspace_id: request.workspace_id, snapshot_sequence: sequence, projects };
      }
      default: throw new Fault(404, "not_found");
    }
  }
}
