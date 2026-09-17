// Local-only instrumentation. Never included in the deployed Worker entry point.
import worker from "../cloudflare/control-plane/src/index";
import { Workspace as SqlInstrumentedWorkspace } from "./free-plan-benchmark-worker";

type R2Metrics = {
  calls: Record<string, number>;
  uploadedBytes: number;
};

let r2Metrics: R2Metrics = { calls: {}, uploadedBytes: 0 };

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
        return member.apply(target, args);
      };
    },
  });
}

export class Workspace extends SqlInstrumentedWorkspace {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, { ...env, ARTIFACTS: tracedBucket(env.ARTIFACTS) });
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
    return worker.fetch(request, { ...env, ARTIFACTS: tracedBucket(env.ARTIFACTS) }, ctx);
  },
} satisfies ExportedHandler<Env>;
