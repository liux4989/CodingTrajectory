/** Real workerd, SQLite, R2 and service-binding qualification with synthetic identities.
 * No Cloudflare account, deployed Worker, host scheduler or provider data is used.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(resolve(root, "cloudflare/control-plane/package.json"));
const { Miniflare, Log, LogLevel, convertV4MiniflareOptions } = require("miniflare");
const { build } = require("esbuild");
const { generateKeyPair, exportJWK, SignJWT } = await import(pathToFileURL(resolve(root,
  "packages/plugins/datahub/web/node_modules/jose/dist/webapi/index.js")));
const workspace = "00000000-0000-0000-0000-000000000001";
const agent = "00000000-0000-0000-0000-000000000003";
const tokens = {
  owner: "local-qualification-owner-token-0000000001",
  reader: "local-qualification-reader-token-000000001",
  worker: "local-qualification-worker-token-000000001",
  other: "local-qualification-other-token-0000000001",
};
const registry = Object.fromEntries(Object.entries(tokens).map(([role, token]) => [
  createHash("sha256").update(token).digest("hex"), {
    workspace_id: role === "other" ? "00000000-0000-0000-0000-000000000002" : workspace,
    agent_id: agent, roles: [role === "worker" ? "estimate_worker" : role === "reader" ? "read" : "owner"],
  },
]));
const { publicKey, privateKey } = await generateKeyPair("RS256", { extractable: true });
const jwk = { ...await exportJWK(publicKey), kid: "local-refactor-qualification" };
const team = "https://qualification.cloudflareaccess.com";
const access = await new SignJWT({ email: "fixture@example.invalid" })
  .setProtectedHeader({ alg: "RS256", kid: jwk.kid }).setIssuer(team)
  .setAudience("local-qualification").setExpirationTime("30m").sign(privateKey);
const authorityVersion = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const datahubVersion = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
let publicCoreCalls = 0;
let deniedEgress = 0;
const outboundService = request => {
  if (request.url === `${team}/cdn-cgi/access/certs`) return Response.json({ keys: [jwk] });
  if (request.url.includes("/v1/core")) publicCoreCalls++;
  deniedEgress++;
  return new Response("fixture egress denied", { status: 503 });
};

async function bundle(path) {
  const result = await build({ entryPoints: [resolve(root, path)], bundle: true,
    write: false, format: "esm", platform: "browser", external: ["cloudflare:*", "node:*"],
    logLevel: "silent" });
  return result.outputFiles[0].text;
}
// At most two compilation jobs.
const [authority, datahub] = await Promise.all([
  bundle("cloudflare/control-plane/src/index.ts"),
  bundle("packages/plugins/datahub/web/worker/live.ts"),
]);
const common = { modules: true, compatibilityDate: "2026-09-10",
  compatibilityFlags: ["nodejs_compat"], outboundService };
const hub = { ...common, script: datahub,
  serviceBindings: { CORE: "authority", ASSETS: () => new Response("synthetic asset") },
  bindings: { CT_WORKSPACE_ID: workspace, CT_CORE_READ_TOKEN: tokens.reader,
    CF_ACCESS_TEAM_DOMAIN: team, CF_ACCESS_AUD: "local-qualification",
    WORKER_VERSION: { id: datahubVersion } } };
const runtime = new Miniflare(convertV4MiniflareOptions({ host: "127.0.0.1", port: 8794, log: new Log(LogLevel.ERROR),
  workers: [
    { ...common, name: "fixture-gateway", serviceBindings: { CORE: "authority", HUB: "datahub" },
      bindings: { ACCESS_JWT: access }, script: `export default {async fetch(request,env) {
        if(new URL(request.url).pathname==='/v1/core')return env.CORE.fetch(request);
        const headers=new Headers(request.headers);
        headers.delete('cf-access-jwt-assertion');
        if(headers.get('CF-Access-Client-Id')==='fixture-id' && headers.get('CF-Access-Client-Secret')==='fixture-secret')
          headers.set('cf-access-jwt-assertion',env.ACCESS_JWT);
        return env.HUB.fetch(new Request(request,{headers}));
      }}` },
    { ...common, name: "authority", script: authority,
      bindings: { CT_PRINCIPALS: JSON.stringify(registry), WORKER_VERSION: { id: authorityVersion } },
      durableObjects: { WORKSPACES: { className: "Workspace", useSQLite: true } },
      r2Buckets: ["ARTIFACTS"] },
    { ...hub, name: "datahub" },
    { ...hub, name: "datahub-invalid-reader", bindings: { ...hub.bindings, CT_CORE_READ_TOKEN: tokens.other } },
  ] }));
const results = [];
async function run(script, executable = "uv", args = ["run", "python"]) {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !key.startsWith("CT_") && !key.startsWith("CLOUDFLARE_") && !key.startsWith("CF_ACCESS_")));
  env.WRANGLER_SEND_METRICS = "false";
  const result = await new Promise((accept, reject) => {
    const child = spawn(executable, [...args, resolve(root, "scripts", script)],
      { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    const timeout = setTimeout(() => child.kill("SIGKILL"), 180000);
    child.stdout.on("data", data => { stdout = (stdout + data).slice(-12000); });
    child.stderr.on("data", data => { stderr = (stderr + data).slice(-4000); });
    child.on("error", error => { clearTimeout(timeout); reject(error); });
    child.on("exit", code => { clearTimeout(timeout); accept({ code, stdout, stderr }); });
  });
  assert.equal(result.code, 0, `${script}: ${result.stderr}`);
  results.push({ script, result: result.stdout.trim() });
}

try {
  await runtime.ready;
  await run("qualify-cloudflare-control-plane.py");
  await run("qualify-incremental-upload.py");
  if (process.argv.includes("--all")) {
    await run("qualify-canonical-repository.py");
    await run("qualify-preparation-reuse.py");
    await run("qualify-managed-collection.py");
    await run("qualify-connection-workflows.py");
    await run("qualify-published-catalog.mjs", "node", []);
  }
  await run("qualify-live-datahub.py");
  await run("qualify-release-workflow.py");
  const worker = await runtime.getWorker("datahub");
  const rejectedReader = await runtime.getWorker("datahub-invalid-reader");
  async function query(method, params = {}, target = worker, token = access) {
    return target.fetch("https://datahub.invalid/api/datahub/query", {
      method: "POST", headers: token ? { "cf-access-jwt-assertion": token } : {},
      body: JSON.stringify({ protocol: "ct.datahub.v1", method, params }),
    });
  }
  assert.equal((await query("datahub.snapshot", {}, worker, null)).status, 403);
  const first = await (await query("datahub.snapshot")).json();
  assert(first.ok && first.data.revision > 0);
  assert.equal(first.data.source_status.ready, null);
  assert.equal(first.data.catching_up, null);
  assert.equal(first.data.freshness.lag_seconds, null);
  assert(Number.isFinite(Date.parse(first.data.freshness.last_publication_at)));
  assert(Number.isFinite(Date.parse(first.data.freshness.authority_observed_at)));
  const core = await runtime.getWorker("authority");
  for (const method of ["chunkDescriptors", "indexStage", "completeStage"]) {
    const denied = await core.fetch("https://core.internal/v1/core", { method: "POST",
      headers: { Authorization: `Bearer ${tokens.owner}` },
      body: JSON.stringify({ protocol: "ct.core.v1", method, params: { workspace_id: workspace } }) });
    assert.equal(denied.status, 404);
  }
  const sessions = await (await query("sessions")).json();
  assert(sessions.ok && sessions.data.items.length > 0);
  const wrongWorkspace = await (await query("datahub.snapshot", {}, rejectedReader)).json();
  assert.equal(wrongWorkspace.ok, false);
  const write = await (await query("ct_collector_publish_artifacts")).json();
  assert.equal(write.ok, false);
  await run("qualify-cloudflare-control-plane.py");
  const newer = await (await query("datahub.snapshot")).json();
  assert(newer.ok && newer.data.revision > first.data.revision);
  const changes = await (await query("datahub.changes", { after_revision: first.data.revision })).json();
  assert(changes.ok && changes.data.invalidations.length > 0);
  assert.equal(publicCoreCalls, 0);
  assert.equal(deniedEgress, 0);
  console.log(JSON.stringify({ passed: true, scripts: results,
    native_binding_checks: 17, public_core_calls: publicCoreCalls, deployed: false }));
} finally {
  await runtime.dispose();
}
