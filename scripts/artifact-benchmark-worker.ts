// Local-only instrumentation. Never included in the deployed Worker entry point.
import worker from "../cloudflare/control-plane/src/index";
import { Workspace as SqlInstrumentedWorkspace } from "./free-plan-benchmark-worker";
import { initializeArtifacts } from "../cloudflare/control-plane/src/artifacts";
import { expandManifest } from "../cloudflare/control-plane/src/artifact-manifest";

type R2Metrics = {
  calls: Record<string, number>;
  uploadedBytes: number;
};

let r2Metrics: R2Metrics = { calls: {}, uploadedBytes: 0 };
let listGate: { entered: boolean; released: boolean } | undefined;
let objectGate: { operation: string; key: string; entered: boolean; released: boolean; fail: boolean } | undefined;
let r2DelayMs = 0;
const invocations: Record<string, number> = {};

function tracedBucket(bucket: R2Bucket): R2Bucket {
  return new Proxy(bucket, {
    get(target, name) {
      const member = Reflect.get(target, name, target);
      if (typeof member !== "function") return member;
      return (...args: unknown[]) => {
        const operation = String(name);
        r2Metrics.calls[operation] = (r2Metrics.calls[operation] ?? 0) + 1;
        if (operation === "put") {
          const body = args[1];
          if (body instanceof Uint8Array) r2Metrics.uploadedBytes += body.byteLength;
          else if (typeof body === "string") r2Metrics.uploadedBytes += new TextEncoder().encode(body).byteLength;
        }
        if (operation === "list" && listGate && !listGate.entered) {
          listGate.entered = true;
          return (async () => {
            while (listGate && !listGate.released) await scheduler.wait(1);
            return member.apply(target, args);
          })();
        }
        if (objectGate && operation === objectGate.operation && args[0] === objectGate.key && !objectGate.entered) {
          const gate = objectGate;
          return (async () => {
            const result = gate.fail ? undefined : await member.apply(target, args);
            gate.entered = true;
            while (!gate.released) await scheduler.wait(1);
            if (gate.fail) throw new Error("local injected R2 failure");
            return result;
          })();
        }
        return r2DelayMs ? scheduler.wait(r2DelayMs).then(() => member.apply(target, args)) : member.apply(target, args);
      };
    },
  });
}

export class Workspace extends SqlInstrumentedWorkspace {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, { ...env, ARTIFACTS: tracedBucket(env.ARTIFACTS) });
  }

  async invoke(method: string, envelope: string, principal: string) {
    invocations[method] = (invocations[method] ?? 0) + 1;
    return super.invoke(method, envelope, principal);
  }

  async rpc(method: string, envelope: any, principal: any) {
    if (method === "ct_internal_artifact_reject") throw new Error("local rejected invocation");
    if (method === "ct_collector_publish_artifacts" && (this.env as any).LOCAL_PUBLICATION_INPUT_GATE) {
      return this.ctx.blockConcurrencyWhile(() => super.rpc(method, envelope, principal));
    }
    return super.rpc(method, envelope, principal);
  }

  /** Disposable qualification hook; this class is never the deployed entry point. */
  setArtifactClaimExpiry(kind: string, sha256: string, expiresAt: number): number {
    return this.ctx.storage.sql.exec(
      "UPDATE artifact_upload_claims SET expires_at=? WHERE kind=? AND sha256=?",
      expiresAt, kind, sha256,
    ).rowsWritten;
  }

  artifactClaimProbe(value: any) {
    const sql = this.ctx.storage.sql;
    if (value.action === "legacy-manifests") {
      for (const row of sql.exec<{ project_id: string; publication_sequence: number; manifest: string }>("SELECT project_id,publication_sequence,manifest FROM artifact_manifests").toArray()) {
        sql.exec("UPDATE artifact_manifests SET manifest=? WHERE project_id=? AND publication_sequence=?",
          JSON.stringify(expandManifest(JSON.parse(row.manifest))), row.project_id, row.publication_sequence);
      }
    } else if (value.action === "legacy") {
      sql.exec("DROP TABLE artifact_upload_claims; CREATE TABLE artifact_upload_claims(kind TEXT NOT NULL, sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(kind,sha256))");
      sql.exec("INSERT INTO artifact_upload_claims VALUES(?,?,?)", value.kind, value.sha256, 4102444800);
      initializeArtifacts((this as any).state);
    } else if (value.action === "delete") {
      sql.exec("DELETE FROM artifact_upload_claims WHERE kind=? AND sha256=?", value.kind, value.sha256);
    } else if (value.action === "completion") {
      sql.exec("UPDATE artifact_upload_claims SET completion=? WHERE kind=? AND sha256=?",
        value.completion == null ? null : JSON.stringify(value.completion), value.kind, value.sha256);
    } else if (value.action === "noise") {
      for (let n = 1; n <= 1000; n++) sql.exec("INSERT INTO artifact_upload_claims VALUES('facts',?,4102444800,'noise',NULL)", n.toString(16).padStart(64, '0'));
    } else if (value.action === "clear-noise") {
      sql.exec("DELETE FROM artifact_upload_claims WHERE token='noise'");
    }
    return sql.exec("SELECT * FROM artifact_upload_claims WHERE kind=? AND sha256=?", value.kind, value.sha256).toArray();
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/__benchmark/invocations") return Response.json(invocations);
    if (url.pathname === "/__benchmark/r2") {
      const result = structuredClone(r2Metrics);
      if (url.searchParams.has("reset")) r2Metrics = { calls: {}, uploadedBytes: 0 };
      if (url.searchParams.has("delay")) r2DelayMs = Number(url.searchParams.get("delay"));
      return Response.json(result);
    }
    if (url.pathname === "/__benchmark/claim-probe") {
      const value = await request.json<any>();
      const stub = env.WORKSPACES.getByName("00000000-0000-0000-0000-000000000001") as any;
      return Response.json(await stub.artifactClaimProbe(value));
    }
    if (url.pathname === "/__benchmark/object-gate") {
      if (request.method === "POST") objectGate = { ...await request.json<any>(), entered: false, released: false };
      else if (request.method === "DELETE" && objectGate) objectGate.released = true;
      return Response.json({ entered: objectGate?.entered ?? false });
    }
    if (url.pathname === "/__benchmark/internal") {
      const value = await request.json<any>();
      const stub = env.WORKSPACES.getByName("00000000-0000-0000-0000-000000000001");
      if (value.method === "ct_internal_artifact_reject") {
        try { await stub.invoke(value.method, JSON.stringify({ request: value.request }), "{}"); }
        catch { return Response.json({ rejected: true }); }
        throw new Error("expected invocation rejection");
      }
      return new Response(await stub.invoke(value.method, JSON.stringify({ request: value.request }),
        JSON.stringify({ workspace_id: value.request.workspace_id, agent_id: "00000000-0000-0000-0000-000000000002", roles: ["owner"] })));
    }
    if (url.pathname === "/__benchmark/list-gate") {
      if (request.method === "POST") {
        listGate = { entered: false, released: false };
      } else if (request.method === "DELETE") {
        if (listGate) listGate.released = true;
      }
      return Response.json({ entered: listGate?.entered ?? false });
    }
    if (url.pathname === "/__benchmark/claim-expiry" && request.method === "POST") {
      const value = await request.json<{ kind: string; sha256: string; expiresAt: number }>();
      const stub = env.WORKSPACES.getByName("00000000-0000-0000-0000-000000000001") as unknown as {
        setArtifactClaimExpiry(kind: string, sha256: string, expiresAt: number): Promise<number>;
      };
      return Response.json({ rowsWritten: await stub.setArtifactClaimExpiry(
        value.kind, value.sha256, value.expiresAt,
      ) });
    }
    return worker.fetch(request, { ...env, ARTIFACTS: tracedBucket(env.ARTIFACTS) }, ctx);
  },
} satisfies ExportedHandler<Env>;
