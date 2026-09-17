#!/usr/bin/env node
// Disposable Miniflare only: no Wrangler config, credentials, or network target.
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const [output, rawGraphCount = '2', rawOrphanCount = '0'] = process.argv.slice(2);
const graphCount = Number(rawGraphCount);
const orphanCount = Number(rawOrphanCount);
if (!output || !Number.isSafeInteger(graphCount) || graphCount < 1 || graphCount > 512
  || !Number.isSafeInteger(orphanCount) || orphanCount < 0 || orphanCount > 5000) {
  throw Error('usage: node scripts/benchmark-artifact-publication.mjs OUTPUT [GRAPHS=2] [ORPHANS=0]');
}
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value !== null && typeof value === 'object'
  ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}` : JSON.stringify(value);
const encoded = value => Buffer.from(stable(value));

const bundle = await build({
  entryPoints: [`${root}scripts/artifact-benchmark-worker.ts`],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  external: ['cloudflare:workers'],
});
const workspace = '00000000-0000-0000-0000-000000000001';
const agent = '00000000-0000-0000-0000-000000000003';
const token = 'local-artifact-benchmark-token-00000001';
const principal = { workspace_id: workspace, agent_id: agent, roles: ['owner', 'read', 'collect'] };
const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{
  name: 'artifact-benchmark', modules: true, script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-09-10',
  durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } },
  r2Buckets: ['ARTIFACTS'],
  bindings: {
    CT_CURSOR_KEY: 'local-artifact-benchmark-cursor-key',
    CT_PRINCIPALS: JSON.stringify({ [sha(token)]: principal }),
  },
}] }));

let requests = 0;
let runtimePid;
function runtimeUsage() {
  if (process.platform !== 'darwin' || !runtimePid) return null;
  const [time, rss] = execFileSync('ps', ['-p', String(runtimePid), '-o', 'time=', '-o', 'rss='], { encoding: 'utf8' }).trim().split(/\s+/);
  const pieces = time.split(':').map(Number);
  return { cpu_ms: pieces.reduce((sum, part) => sum * 60 + part, 0) * 1000, rss_bytes: Number(rss) * 1024 };
}
function findRuntimePid() {
  if (process.platform !== 'darwin') return undefined;
  const rows = execFileSync('ps', ['-axo', 'pid=,ppid=,comm='], { encoding: 'utf8' }).trim().split('\n')
    .map(line => line.trim().split(/\s+/, 3))
    .map(([pid, parent, command]) => ({ pid: Number(pid), parent: Number(parent), command }));
  const descendants = new Set([process.pid]);
  for (let pass = 0; pass < rows.length; pass++) {
    for (const row of rows) if (descendants.has(row.parent)) descendants.add(row.pid);
  }
  return rows.find(row => descendants.has(row.pid) && row.command?.endsWith('/workerd'))?.pid;
}
async function fetch(path, init = {}) {
  requests++;
  return mf.dispatchFetch(`http://local${path}`, init);
}
async function rpc(method, params, key) {
  const response = await fetch('/v1/core', {
    method: 'POST', headers: { authorization: `Bearer ${token}` },
    body: JSON.stringify({ protocol: 'ct.core.v1', method,
      params: { workspace_id: workspace, ...params }, ...(key ? { idempotency_key: key } : {}) }),
  });
  const value = await response.json();
  if (!value.ok) throw Error(`${method}: ${value.error?.code}`);
  const sql = value.data.__benchmark;
  if (!sql) throw Error(`${method}: local SQL instrumentation unavailable`);
  delete value.data.__benchmark;
  return { data: value.data, sql };
}
async function r2Metrics(reset = false) {
  const response = await mf.dispatchFetch(`http://local/__benchmark/r2${reset ? '?reset' : ''}`);
  return response.json();
}
async function retained() {
  const bucket = await mf.getR2Bucket('ARTIFACTS');
  let cursor;
  let bytes = 0;
  let entries = 0;
  do {
    const page = await bucket.list({ cursor });
    entries += page.objects.length;
    bytes += page.objects.reduce((sum, object) => sum + object.size, 0);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return { entries, bytes };
}
function aggregateSql(samples) {
  const result = { calls: 0, rowsRead: 0, rowsWritten: 0, logicalChanges: 0, groups: {} };
  for (const sample of samples) {
    result.logicalChanges += sample.logical_changes;
    for (const [label, group] of Object.entries(sample.groups)) {
      const target = result.groups[label] ??= { calls: 0, rowsRead: 0, rowsWritten: 0 };
      for (const key of Object.keys(target)) target[key] += group[key];
      result.calls += group.calls; result.rowsRead += group.rowsRead; result.rowsWritten += group.rowsWritten;
    }
  }
  return result;
}
async function scenario(name, run) {
  await r2Metrics(true);
  const beforeRequests = requests;
  const before = runtimeUsage();
  let peakRss = before?.rss_bytes ?? 0;
  const sampler = before ? setInterval(() => { peakRss = Math.max(peakRss, runtimeUsage().rss_bytes); }, 50) : null;
  const started = performance.now();
  let sql = [];
  try { sql = await run(); }
  finally { if (sampler) clearInterval(sampler); }
  const after = runtimeUsage();
  const result = {
    scenario: name,
    httpRequests: requests - beforeRequests,
    wallMs: performance.now() - started,
    processCpuMs: before && after ? after.cpu_ms - before.cpu_ms : null,
    processRssSampleMax: after ? Math.max(peakRss, after.rss_bytes) : null,
    sql: aggregateSql(sql),
    r2: await r2Metrics(),
    retained: await retained(),
  };
  report.results.push(result);
  return result;
}

function artifact(kind, graphId, revision) {
  const body = kind === 'facts' ? {
    schema_version: 'ct.published_facts.v1', graph_id: graphId,
    fact_set_digest: sha(`facts:${graphId}:${revision}`), kind_counts: { graph: 1 },
    rows: [{ kind: 'graph', fact_id: graphId, payload: { synthetic: true, revision } }],
  } : {
    schema_version: 'ct.prepared-summary.v1', preparation_version: 'ct.graph-preparation.v1',
    graph_id: graphId, fact_set_digest: sha(`facts:${graphId}:${revision}`), aliases: [graphId],
    project_sessions: [{ project: 'Artifact benchmark', lineage_root_session_id: graphId }],
  };
  const bytes = encoded(body);
  return { kind, body: bytes, reference: { kind, sha256: sha(bytes), bytes: bytes.length } };
}

const report = {
  head: git('rev-parse', 'HEAD'),
  localDiff: git('diff', '--stat'),
  recordedAt: new Date().toISOString(),
  synthetic: true,
  scope: { graphs: graphCount, rowsPerGraph: 1, seededOrphans: orphanCount },
  units: 'Disposable local workerd SQL cursor counters, instrumented R2 method calls, HTTP request count, wall time; process CPU/RSS only on macOS and not Cloudflare billing or isolate limits',
  harnessSha256: Object.fromEntries(['benchmark-artifact-publication.mjs', 'artifact-benchmark-worker.ts'].map(name =>
    [name, sha(readFileSync(`${root}scripts/${name}`))])),
  environment: { node: process.version, platform: process.platform, arch: process.arch,
    miniflare: require('miniflare/package.json').version, workerd: require('workerd/package.json').version },
  results: [],
};

try {
  const project = (await rpc('ct_project_register', { agent_id: agent, display_name: 'Artifact benchmark' }, 'project')).data;
  const source = (await rpc('ct_collector_register_source', { agent_id: agent, project_id: project.project_id,
    vendor: 'amp', native_session_id: 'artifact-benchmark' }, 'source')).data;
  runtimePid = findRuntimePid();
  report.environment.processSampling = runtimePid ? 'macOS ps cumulative process CPU and 50 ms RSS samples; shared workerd process, not isolate heap or billed CPU' : 'unavailable';
  const graphs = Array.from({ length: graphCount }, (_, index) =>
    `00000000-0000-0000-0000-${String(index + 1).padStart(12, '0')}`);
  let sourceSequence = -1;
  async function checkpoint(revision) {
    sourceSequence++;
    const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [revision + 1] }, session_digest: sha(`session:${revision}`) };
    const content = sha(stable(payload));
    const result = await rpc('ct_collector_publish_observation', { agent_id: agent, ...source,
      source_sequence: sourceSequence, event_id: `checkpoint:${content}`, parser_version: 'benchmark.v1',
      content_sha256: content, observed_at: '2026-09-17T00:00:00Z', payload }, `checkpoint:${revision}`);
    return { vector: { ...source, source_sequence: sourceSequence, content_sha256: content }, sql: result.sql };
  }
  function prepared(revisions) {
    return graphs.map((graphId, index) => {
      const facts = artifact('facts', graphId, revisions[index]);
      const summary = artifact('summary', graphId, revisions[index]);
      return { graphId, facts, summary, publication: {
        graph_id: graphId, graph_input_sha256: sha(`input:${graphId}:${revisions[index]}`),
        fact_schema_version: 'ct.published_facts.v1', fact_set_digest: sha(`facts:${graphId}:${revisions[index]}`),
        fact_count: 1, source_ids: [source.source_id], vendors: ['amp'], observed_at: '2026-09-17T00:00:00Z',
        facts: facts.reference, summary: summary.reference,
      } };
    });
  }
  async function upload(values) {
    for (const graph of values) for (const value of [graph.facts, graph.summary]) {
      const response = await fetch(`/v1/artifacts/${value.kind}/${value.reference.sha256}`, {
        method: 'PUT', headers: { authorization: `Bearer ${token}` }, body: value.body,
      });
      if (!response.ok) throw Error(`upload failed: ${response.status}`);
    }
  }
  async function publish(sequence, values, vector) {
    return rpc('ct_collector_publish_artifacts', { agent_id: agent, project_id: project.project_id,
      publication_sequence: sequence, inventory_state: 'complete', source_vector: [vector],
      graphs: values.map(value => value.publication) }, `publication:${sequence}`);
  }

  let values = prepared(Array(graphCount).fill(0));
  let point = await checkpoint(0);
  if (orphanCount) {
    const bucket = await mf.getR2Bucket('ARTIFACTS');
    for (let index = 0; index < orphanCount; index++) {
      await bucket.put(`workspaces/${workspace}/artifacts/facts/orphan-${String(index).padStart(8, '0')}`, 'x');
    }
  }
  await scenario('initial_publication', async () => { await upload(values); const result = await publish(0, values, point.vector); return [result.sql]; });
  await scenario('unchanged_collector_run', async () => []);
  values = prepared([1, ...Array(Math.max(graphCount - 1, 0)).fill(0)]);
  point = await checkpoint(1);
  await scenario('one_changed_graph', async () => { await upload([values[0]]); const result = await publish(1, values, point.vector); return [result.sql]; });
  if (graphCount <= 2) await scenario('prepared_summary_and_selected_detail_reads', async () => {
    const sql = [];
    const manifest = await rpc('ct_artifact_manifest', { snapshot_sequence: null }); sql.push(manifest.sql);
    for (const graph of manifest.data.manifests[0].graphs) {
      const result = await rpc('ct_artifact_read', { snapshot_sequence: manifest.data.snapshot_sequence,
        kind: 'summary', sha256: graph.summary.sha256 }); sql.push(result.sql);
    }
    const selected = manifest.data.manifests[0].graphs[0];
    const detail = await rpc('ct_artifact_read', { snapshot_sequence: manifest.data.snapshot_sequence,
      kind: 'facts', sha256: selected.facts.sha256 }); sql.push(detail.sql);
    return sql;
  });
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report.results, null, 2));
} finally {
  await mf.dispose();
}
