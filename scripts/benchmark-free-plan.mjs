#!/usr/bin/env node
// Disposable Miniflare only: no Wrangler config, credentials, or network target.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const baseline = '1d9bc2851ee7667969164016d36a504944bf9362';
const [input, output] = process.argv.slice(2);
if (!input || !output) throw Error('usage: node scripts/benchmark-free-plan.mjs INPUT OUTPUT');
const raw = readFileSync(input);
const corpus = JSON.parse(raw);
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
const sourceHashes = {};
const bundle = await build({
  entryPoints: [`${root}scripts/free-plan-benchmark-worker.ts`], bundle: true,
  write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'],
  plugins: [{ name: 'pinned-source', setup(build) {
    build.onLoad({ filter: /cloudflare\/control-plane\/src\/.*\.(ts|js)$/ }, args => {
      const path = args.path.slice(root.length);
      // validators.js is generated/ignored; record its exact local bytes too.
      const contents = path.endsWith('/validators.js') ? readFileSync(args.path, 'utf8')
        : execFileSync('git', ['show', `${baseline}:${path}`], { cwd: root, encoding: 'utf8' });
      sourceHashes[path] = createHash('sha256').update(contents).digest('hex');
      return { contents, loader: path.endsWith('.ts') ? 'ts' : 'js' };
    });
  } }],
});
const workspace = '00000000-0000-0000-0000-000000000001';
const agent = '00000000-0000-0000-0000-000000000003';
const token = 'local-qualification-owner-token-0000000001';
const principal = { workspace_id: workspace, agent_id: agent, roles: ['owner'] };
const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ name: 'benchmark', modules: true, script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-09-10', durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } },
  bindings: { CT_CURSOR_KEY: 'local-benchmark-cursor-key-000000000000',
    CT_PRINCIPALS: JSON.stringify({ [createHash('sha256').update(token).digest('hex')]: principal }) },
}] }));
const report = { baseline, head: git('rev-parse', 'HEAD'), local_diff: git('diff', '--stat', baseline, 'HEAD'),
  input_sha256: createHash('sha256').update(raw).digest('hex'), sourceHashes,
  synthetic: corpus.synthetic === true, recorded_at: new Date().toISOString(),
  harness_sha256: Object.fromEntries(['benchmark-free-plan.mjs', 'free-plan-benchmark-worker.ts', 'prepare-free-plan-benchmark.py'].map(name =>
    [name, createHash('sha256').update(readFileSync(`${root}scripts/${name}`)).digest('hex')])),
  environment: { node: process.version, platform: process.platform, arch: process.arch,
    miniflare: require('miniflare/package.json').version, workerd: require('workerd/package.json').version },
  units: 'Local workerd sql.exec cursor counters, SQLite total_changes, loopback wall ms; not deployed billing/CPU or isolate memory',
  results: [],
};
let runtimePid;
function runtimeUsage() {
  if (process.platform !== 'darwin' || !runtimePid) return null;
  const [time, rss] = execFileSync('ps', ['-p', String(runtimePid), '-o', 'time=', '-o', 'rss='], { encoding: 'utf8' }).trim().split(/\s+/);
  const pieces = time.split(':').map(Number);
  return { cpu_ms: pieces.reduce((sum, part) => sum * 60 + part, 0) * 1000, rss_bytes: Number(rss) * 1024 };
}
function aggregate(samples) {
  const result = { requests: samples.length, wall_ms: 0, logical_changes: 0, rowsRead: 0, rowsWritten: 0,
    process_cpu_ms: samples.every(s => s.process_cpu_ms != null) ? 0 : null,
    process_rss_sample_max: samples.every(s => s.process_rss_sample_max != null) ? 0 : null, groups: {} };
  for (const sample of samples) {
    result.wall_ms += sample.wall_ms;
    result.logical_changes += sample.logical_changes;
    if (result.process_cpu_ms != null) result.process_cpu_ms += sample.process_cpu_ms;
    if (result.process_rss_sample_max != null) result.process_rss_sample_max = Math.max(result.process_rss_sample_max, sample.process_rss_sample_max);
    result.database_bytes = sample.database_bytes;
    for (const [label, group] of Object.entries(sample.groups)) {
      const target = result.groups[label] ??= { calls: 0, rowsRead: 0, rowsWritten: 0 };
      for (const key of Object.keys(target)) target[key] += group[key];
      result.rowsRead += group.rowsRead; result.rowsWritten += group.rowsWritten;
    }
  }
  return result;
}
async function rpc(method, params, key) {
  const usage = runtimeUsage();
  let peakRss = usage?.rss_bytes ?? 0;
  const sampler = usage ? setInterval(() => { peakRss = Math.max(peakRss, runtimeUsage().rss_bytes); }, 50) : null;
  const start = performance.now();
  let response;
  try { response = await mf.dispatchFetch('http://local/v1/core', { method: 'POST',
    headers: { authorization: `Bearer ${token}` },
    body: JSON.stringify({ protocol: 'ct.core.v1', method, params: { workspace_id: workspace, ...params },
      ...(key ? { idempotency_key: key } : {}) }),
  }); } finally { if (sampler) clearInterval(sampler); }
  const result = await response.json();
  const wall_ms = performance.now() - start;
  const after = runtimeUsage();
  if (!result.ok) throw Error(`${method}: ${result.error?.code}`);
  const metrics = result.data.__benchmark;
  delete result.data.__benchmark;
  return { data: result.data, metrics: { ...metrics, wall_ms,
    process_cpu_ms: after && usage ? after.cpu_ms - usage.cpu_ms : null,
    process_rss_sample_max: Math.max(peakRss, after?.rss_bytes ?? 0) } };
}
try {
  const namespace = await mf.getDurableObjectNamespace('WORKSPACES');
  const processes = execFileSync('ps', ['-axo', 'pid,ppid,comm'], { encoding: 'utf8' });
  runtimePid = processes.split('\n').map(line => line.trim().split(/\s+/))
    .find(([, parent, command]) => Number(parent) === process.pid && command?.endsWith('/workerd'))?.[0];
  report.environment.process_sampling = runtimePid ? 'macOS ps process CPU (10 ms resolution) and 50 ms RSS samples; shared workerd process, NOT isolate heap/Worker billed CPU' : 'unavailable';
  const probe = namespace.get(namespace.idFromName('counter-calibration'));
  report.counter_calibration = JSON.parse(await probe.invoke('benchmark_counter_calibration', '{}', '{}')).body;
  if (report.counter_calibration.rows_after_rollback !== 0) throw Error('rollback failed');
  const history = namespace.get(namespace.idFromName('history-benchmark'));
  for (const [start, end] of [[1, 1], [2, 20], [21, 100]]) {
    const before = performance.now();
    const result = JSON.parse(await history.invoke('benchmark_history', JSON.stringify({ start, end }), '{}')).body;
    if (result.retained !== end * 100 || result.current !== 100) throw Error('history count mismatch');
    report.results.push({ scenario: 'history_sql_only', revisions: end, retained: result.retained, current: result.current,
      ...aggregate([{ ...result.__benchmark, wall_ms: performance.now() - before }]) });
  }
  const living = namespace.get(namespace.idFromName('living-benchmark'));
  for (const [start, count] of [[1, 1000], [1001, 9000], [10001, 1]]) {
    const before = performance.now();
    const write = JSON.parse(await living.invoke('benchmark_living', JSON.stringify({ start, count }), '{}'));
    const writeMs = performance.now() - before;
    const readStart = performance.now();
    const read = JSON.parse(await living.invoke('benchmark_living_read', '{}', '{}'));
    if (read.status !== (start + count - 1 > 10000 ? 413 : 200)) throw Error('living boundary mismatch');
    report.results.push({ scenario: 'living', observations: start + count - 1,
      write: aggregate([{ ...write.body.__benchmark, wall_ms: writeMs }]),
      read: { status: read.status, error: read.body.error, ...aggregate([{ ...read.body.__benchmark, wall_ms: performance.now() - readStart }]) } });
  }
  const project = (await rpc('ct_project_register', { agent_id: agent, display_name: corpus.project_name })).data;
  const source = (await rpc('ct_collector_register_source', { agent_id: agent, project_id: project.project_id,
    vendor: 'amp', native_session_id: 'free-plan-benchmark' }, 'source')).data;
  const observed_at = '2026-09-17T00:00:00Z';
  const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] }, session_digest: 'a'.repeat(64) };
  const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value !== null && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}` : JSON.stringify(value);
  const content_sha256 = createHash('sha256').update(stable(payload)).digest('hex');
  await rpc('ct_collector_publish_observation', { agent_id: agent, ...source, source_sequence: 0,
    event_id: `checkpoint:${content_sha256}`, parser_version: 'benchmark.v1', content_sha256, observed_at, payload }, 'checkpoint');
  const batches = [];
  for (const f of corpus.fact_sets) {
    const chunks = []; let rows = [], bytes = 2;
    for (const row of f.rows) {
      const rowBytes = Buffer.byteLength(stable(row));
      if (rows.length && (rows.length === 512 || bytes + rowBytes + 1 > 2 * 1024 * 1024)) { chunks.push(rows); rows = []; bytes = 2; }
      bytes += rowBytes + (rows.length ? 1 : 0); rows.push(row);
    }
    if (rows.length) chunks.push(rows);
    chunks.forEach((rows, batch_index) => batches.push({ agent_id: agent, graph_id: f.graph_id,
      fact_set_digest: f.fact_set_digest, batch_index, batch_count: chunks.length, rows }));
  }
  report.scope = { graphs: corpus.fact_sets.length, rows: corpus.fact_sets.reduce((n, f) => n + f.rows.length, 0), batches: batches.length,
    encoded_bytes: corpus.fact_sets.reduce((n, f) => n + Buffer.byteLength(stable(f)), 0) };
  async function stage(label) {
    const samples = [];
    for (const batch of batches) samples.push((await rpc('ct_collector_stage_fact_rows', batch)).metrics);
    report.results.push({ scenario: label, ...aggregate(samples) });
  }
  await stage('fresh_stage');
  await stage('identical_stage_retry');
  const request = { agent_id: agent, project_id: project.project_id, publication_sequence: 0,
    source_vector: [{ ...source, source_sequence: 0, content_sha256 }],
    graphs: corpus.fact_sets.map(f => ({ graph_id: f.graph_id, fact_set_digest: f.fact_set_digest,
      fact_count: f.rows.length, schema_version: f.schema_version, kind_counts: f.kind_counts,
      source_ids: [source.source_id], observed_at })) };
  const publication = await rpc('ct_collector_publish_facts', request, 'publication');
  if (publication.data.details.rows_inserted !== report.scope.rows) throw Error('insert count mismatch');
  report.results.push({ scenario: 'publish', details: publication.data.details, ...aggregate([publication.metrics]) });
  const retry = await rpc('ct_collector_publish_facts', request, 'publication');
  if (stable(publication.data) !== stable(retry.data)) throw Error('receipt changed');
  report.results.push({ scenario: 'committed_publication_retry', receipt_identical: true, ...aggregate([retry.metrics]) });
  await stage('unchanged_republication_stage');
  const unchanged = await rpc('ct_collector_publish_facts', { ...request, publication_sequence: 1 }, 'publication-1');
  if (unchanged.data.details.rows_reused !== report.scope.rows || unchanged.data.details.rows_inserted !== 0) throw Error('reuse mismatch');
  report.results.push({ scenario: 'unchanged_republication', details: unchanged.data.details, ...aggregate([unchanged.metrics]) });
  const status = [], snapshots = [];
  for (let i = 0; i < 20; i++) {
    status.push((await rpc('ct_connection_status', {})).metrics.wall_ms);
    snapshots.push((await rpc('ct_workspace_snapshot', {})).metrics.wall_ms);
  }
  report.request_wall_ms = { connection_status: status, workspace_snapshot: snapshots };
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ scope: report.scope, results: report.results.map(({ scenario, rowsWritten, logical_changes, wall_ms, observations, read }) => ({ scenario, rowsWritten, logical_changes, wall_ms, observations, status: read?.status })) }, null, 2));
} finally { await mf.dispose(); }
