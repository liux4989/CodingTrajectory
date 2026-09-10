// Local-only integration benchmark. Run from a worktree with web dependencies
// installed and a frozen web/dist snapshot. Never invokes a remote deployment.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { resolve, dirname, join } from 'node:path';
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const web = join(root, 'packages/plugins/datahub/web');
const require = createRequire(join(web, 'package.json'));
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const { generateKeyPair, exportJWK, SignJWT } = await import(require.resolve('jose'));
const out = resolve(process.argv[2] ?? join(root, '.artifacts/module-registry'));
const rounds = Number(process.env.ROUNDS ?? 12);
assert(Number.isInteger(rounds) && rounds >= 2 && rounds <= 100);
mkdirSync(out, { recursive: true });
const hash = value => createHash('sha256').update(value).digest('hex');
const json = path => JSON.parse(readFileSync(path, 'utf8'));
const save = (name, value) => writeFileSync(join(out, name), JSON.stringify(value, null, 2) + '\n');
const wrangler = join(web, 'node_modules/.bin/wrangler');
const config = json(join(web, '../wrangler.snapshot.jsonc'));
assert(!config.compatibility_flags.some(f => ['new_module_registry', 'legacy_module_registry'].includes(f)), 'Baseline already selects a registry; review the comparison');
const assets = join(web, 'dist');
const inventory = (dir, prefix = '') => readdirSync(dir, { withFileTypes: true }).sort((a,b) => a.name.localeCompare(b.name)).flatMap(e =>
  e.isDirectory() ? inventory(join(dir,e.name), prefix + e.name + '/') : [[prefix + e.name, hash(readFileSync(join(dir,e.name)))]]);
const assetInventory = inventory(assets);
const sourceInventory = inventory(join(web, 'worker'));
const lockHash = hash(readFileSync(join(web, 'bun.lock')));
const manifest = json(join(assets, '_snapshot/manifest.json'));
for (const [name, digest] of Object.entries(manifest.files)) assert.equal(hash(readFileSync(join(assets,'_snapshot',name))), digest);
const run = args => execFileSync(wrangler, args, { cwd: root, encoding: 'utf8', timeout: 90000,
  env: { ...process.env, WRANGLER_SEND_METRICS: 'false' } });
const variants = ['baseline', 'new'];
const bundles = {};
for (const variant of variants) {
  const cfg = { ...config, main: join(web, 'worker/snapshot.ts'), assets: { ...config.assets, directory: assets },
    compatibility_flags: [...config.compatibility_flags, ...(variant === 'new' ? ['new_module_registry'] : [])] };
  save(`${variant}.json`, cfg);
  const log = run(['deploy', '--dry-run', '-c', join(out, `${variant}.json`), '--outdir', join(out, variant), '--metafile', join(out, `${variant}.meta.json`)]);
  writeFileSync(join(out, `${variant}.build.log`), log);
  run(['deploy', '--dry-run', '-c', join(out, `${variant}.json`), '--outfile', join(out, `${variant}.bundle`)]);
  const code = readFileSync(join(out, variant, 'snapshot.js'));
  const meta = json(join(out, `${variant}.meta.json`));
  bundles[variant] = { bytes: code.length, gzip_bytes: gzipSync(code).length, sha256: hash(code),
    outputs: Object.values(meta.outputs).map(o => ({ bytes: o.bytes, imports: o.imports })),
    contributing_inputs: Object.values(meta.outputs).flatMap(o => Object.entries(o.inputs).filter(([,v]) => v.bytesInOutput > 0).map(([name,v]) => ({ name: name.replace(/^.*(?=node_modules\/)/, '').replace(/^.*(?=packages\/plugins\/datahub\/web\/worker\/)/, ''), bytes: v.bytesInOutput }))) };
}
assert.equal(bundles.baseline.sha256, bundles.new.sha256, 'Flag-only build changed application bytes');

const issuer = 'https://benchmark.cloudflareaccess.com';
const { publicKey, privateKey } = await generateKeyPair('RS256');
const jwk = { ...await exportJWK(publicKey), kid: 'benchmark', alg: 'RS256', use: 'sig' };
const token = await new SignJWT({ email: 'synthetic@example.invalid' }).setProtectedHeader({ alg: 'RS256', kid: 'benchmark' })
  .setIssuer(issuer).setAudience('benchmark').setIssuedAt().setExpirationTime('1h').sign(privateKey);
const cases = [
  ['snapshot', '/api/datahub/snapshot', 200], ['sessions', '/api/sessions?limit=3', 200],
  ['projects', '/api/projects', 200], ['changes', '/api/datahub/changes?after_revision=0', 200],
  ['asset', '/', 200], ['hidden', '/_snapshot/snapshot.json', 404],
  ['bad-query', '/api/datahub/snapshot?unexpected=1', 400], ['bad-days', '/api/sessions?since_days=8', 400],
  ['duplicate-query', '/api/sessions?since_days=7&since_days=1', 400],
  ['bad-id', '/api/sessions/graph?session_id=invalid', 400],
  ['content', '/api/sessions/items?include_content=true', 404], ['unknown', '/api/refresh', 404],
  ['method', '/api/sessions', 404, 'POST'], ['no-token', '/api/datahub/snapshot', 403, 'GET', ''],
  ['bad-token', '/api/datahub/snapshot', 403, 'GET', 'invalid'],
];
const signatures = new Map();
const measurements = [];
const profiles = [];
function summary(values) {
  const a = [...values].sort((a,b) => a-b);
  return { n: a.length, median_ms: a.length % 2 ? a[(a.length-1)/2] : (a[a.length/2-1]+a[a.length/2])/2,
    p95_ms: a[Math.ceil(a.length*.95)-1], min_ms: a[0], max_ms: a.at(-1) };
}
// Discard a complete pair to warm the Node driver and filesystem caches.
for (let round = -1; round < rounds; round++) {
  for (const variant of round % 2 ? [...variants].reverse() : variants) {
    // Wrangler's profile dynamically imports the entrypoint. Keep this distinct
    // from full Miniflare readiness and first actual authenticated request.
    const profileLog = run(['check', 'startup', '-c', join(out, `${variant}.json`), '--workerBundle', join(out, `${variant}.bundle`), '--outfile', join(out, `${variant}-${round}.cpuprofile`)]);
    writeFileSync(join(out, `${variant}-${round}.profile.log`), profileLog);
    const active = profileLog.match(/Active: ([\d.]+) ms/);
    assert(active, 'Wrangler startup output changed');
    if (round >= 0) profiles.push({ variant, round, active_ms: Number(active[1]) });
    let jwksCalls = 0;
    const started = performance.now();
    const mf = new Miniflare(convertV4MiniflareOptions({ name: 'registry-benchmark',
      compatibilityDate: config.compatibility_date,
      compatibilityFlags: [...config.compatibility_flags, ...(variant === 'new' ? ['new_module_registry'] : [])],
      modules: [{ type: 'ESModule', path: join(out, variant, 'snapshot.js') }],
      assets: { directory: assets, binding: 'ASSETS', run_worker_first: true, routerConfig: { has_user_worker: true }, assetConfig: { not_found_handling: 'single-page-application' } },
      bindings: { CF_ACCESS_TEAM_DOMAIN: issuer, CF_ACCESS_AUD: 'benchmark' },
      outboundService: request => {
        assert.equal(request.url, `${issuer}/cdn-cgi/access/certs`, 'Unexpected outbound request');
        jwksCalls++;
        return Response.json({ keys: [jwk] });
      },
    }));
    try {
      await mf.ready;
      const ready_ms = performance.now() - started;
      async function request(c) {
        const [name,path,status,method='GET',auth=token] = c;
        const start = performance.now();
        const response = await mf.dispatchFetch(`https://benchmark.invalid${path}`, { method, headers: { 'cf-access-jwt-assertion': auth } });
        const body = await response.arrayBuffer();
        const ms = performance.now() - start;
        assert.equal(response.status, status, `${name}: ${response.status === status ? '' : Buffer.from(body).toString().slice(0, 1500)}`);
        const headers = [...response.headers].filter(([k]) => !['date','etag','last-modified'].includes(k));
        const signature = JSON.stringify([status, headers, hash(Buffer.from(body))]);
        if (signatures.has(name)) assert.equal(signature, signatures.get(name), `Parity: ${name}`);
        else signatures.set(name, signature);
        assert.equal(response.headers.get('cache-control'), 'no-store');
        return ms;
      }
      const first_ms = await request(cases[0]);
      for (const c of cases.slice(1)) await request(c);
      const steady = [];
      for (let i = 0; i < 50; i++) steady.push(await request(cases[0]));
      assert.equal(jwksCalls, 1, 'JWKS cache behavior changed');
      if (round >= 0) measurements.push({ variant, round, ready_ms, first_ms, steady_ms: steady });
      console.error(`${round + 1}/${rounds} ${variant}: ready=${ready_ms.toFixed(1)} first=${first_ms.toFixed(2)} steady=${summary(steady).median_ms.toFixed(2)} ms`);
    } finally { await mf.dispose(); }
  }
}
assert.deepEqual(inventory(assets), assetInventory, 'Assets changed during measurement');
assert.deepEqual(inventory(join(web, 'worker')), sourceInventory, 'Worker sources changed during measurement');
assert.equal(hash(readFileSync(join(web, 'bun.lock'))), lockHash, 'Dependency lock changed during measurement');
const result = { baseline_commit: execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim(),
  measured_at: new Date().toISOString(), worker_source_sha256: hash(JSON.stringify(sourceInventory)), lock_sha256: lockHash,
  node: process.version, platform: process.platform, arch: process.arch,
  versions: Object.fromEntries(['wrangler','miniflare','workerd','jose','esbuild'].map(name => [name, json(require.resolve(`${name}/package.json`)).version])),
  asset_files: assetInventory.length, asset_inventory_sha256: hash(JSON.stringify(assetInventory)),
  manifest_checks: Object.keys(manifest.files).length, bundles, cases: cases.map(([name,,status]) => ({name,status})), profiles, measurements,
  summary: Object.fromEntries(variants.map(variant => { const m = measurements.filter(m => m.variant === variant); return [variant, {
    profile_active: summary(profiles.filter(p=>p.variant===variant).map(p=>p.active_ms)),
    ready: summary(m.map(m=>m.ready_ms)), first_authenticated: summary(m.map(m=>m.first_ms)),
    steady: summary(m.flatMap(m=>m.steady_ms)), steady_per_run_medians: summary(m.map(m=>summary(m.steady_ms).median_ms)),
  }]; })) };
save('results.json', result);
console.log(JSON.stringify(result.summary, null, 2));
