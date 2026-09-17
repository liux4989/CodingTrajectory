// Local-only instrumentation. Never included in the deployed Worker entry point.
import worker from "../cloudflare/control-plane/src/index";
import { Workspace as SqlInstrumentedWorkspace } from "./free-plan-benchmark-worker";

type R2Metrics = {
  calls: Record<string, number>;
  uploadedBytes: number;
};

let r2Metrics: R2Metrics = { calls: {}, uploadedBytes: 0 };
let listGate: { entered: boolean; released: boolean } | undefined;

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
        return member.apply(target, args);
      };
    },
  });
}

export class Workspace extends SqlInstrumentedWorkspace {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, { ...env, ARTIFACTS: tracedBucket(env.ARTIFACTS) });
  }

  /** Disposable qualification hook; this class is never the deployed entry point. */
  setArtifactClaimExpiry(kind: string, sha256: string, expiresAt: number): number {
    return this.ctx.storage.sql.exec(
      "UPDATE artifact_upload_claims SET expires_at=? WHERE kind=? AND sha256=?",
      expiresAt, kind, sha256,
    ).rowsWritten;
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/__benchmark/r2") {
      const result = structuredClone(r2Metrics);
      if (url.searchParams.has("reset")) r2Metrics = { calls: {}, uploadedBytes: 0 };
      return Response.json(result);
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
