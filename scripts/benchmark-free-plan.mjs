#!/usr/bin/env node
// Compatibility benchmark for the retired row-staging corpus against Python workerd.
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';
import { createPythonWorkerRuntime, pythonWorkerSource } from './python-worker-runtime.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const [input, output] = process.argv.slice(2);
if (!input || !output) throw Error('usage: node scripts/benchmark-free-plan.mjs INPUT OUTPUT');
const raw = readFileSync(input);
const corpus = JSON.parse(raw);
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]`
  : value !== null && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
    : JSON.stringify(value);
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
const workspace = '00000000-0000-0000-0000-000000000001';
const agent = '00000000-0000-0000-0000-000000000003';
const token = 'local-qualification-owner-token-0000000001';
const principal = { workspace_id: workspace, agent_id: agent, roles: ['owner'] };
const mf = createPythonWorkerRuntime({ name: 'free-plan-compatibility', bindings: {
  CT_CURSOR_KEY: 'local-benchmark-cursor-key-000000000000',
  CT_PRINCIPALS: JSON.stringify({ [sha(token)]: principal }),
} });

let requestCount = 0;
async function rpc(method, params = {}, key, expected = 200) {
  const started = performance.now();
  requestCount++;
  const response = await mf.dispatchFetch('http://local/v1/core', { method: 'POST',
    headers: { authorization: `Bearer ${token}` },
    body: JSON.stringify({ protocol: 'ct.core.v1', method,
      params: { workspace_id: workspace, ...params }, ...(key ? { idempotency_key: key } : {}) }) });
  const value = await response.json();
  if (response.status !== expected) throw Error(`${method}: expected ${expected}, received ${response.status}: ${JSON.stringify(value)}`);
  return { value, wall_ms: performance.now() - started };
}

const report = {
  head: git('rev-parse', 'HEAD'),
  local_diff: git('diff', '--stat'),
  recorded_at: new Date().toISOString(),
  input_sha256: sha(raw),
  synthetic: corpus.synthetic === true,
  scope: { graphs: corpus.fact_sets.length,
    rows: corpus.fact_sets.reduce((count, factSet) => count + factSet.rows.length, 0) },
  worker_source_sha256: sha(readFileSync(pythonWorkerSource)),
  harness_sha256: Object.fromEntries(['benchmark-free-plan.mjs', 'python-worker-runtime.mjs', 'prepare-free-plan-benchmark.py']
    .map(name => [name, sha(readFileSync(`${root}scripts/${name}`))])),
  units: 'Loopback wall time through the real Python Worker in local workerd.',
  unavailable_metrics: {
    sql_cursor_counters: 'This retired-corpus compatibility harness does not install SQL instrumentation.',
    legacy_row_staging: 'ct_collector_stage_fact_rows and ct_collector_publish_facts are retired; prepared artifact publication supersedes this corpus.',
  },
  results: [],
};

try {
  const project = (await rpc('ct_project_register', { agent_id: agent, display_name: corpus.project_name }, 'project')).value.data;
  const source = (await rpc('ct_collector_register_source', { agent_id: agent, project_id: project.project_id,
    vendor: 'amp', native_session_id: 'free-plan-compatibility' }, 'source')).value.data;
  const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] }, session_digest: 'a'.repeat(64) };
  const content_sha256 = sha(stable(payload));
  await rpc('ct_collector_publish_observation', { agent_id: agent, ...source, source_sequence: 0,
    event_id: `checkpoint:${content_sha256}`, parser_version: 'benchmark.v1', content_sha256,
    observed_at: '2026-09-17T00:00:00Z', payload }, 'checkpoint');
  for (const method of ['ct_collector_stage_fact_rows', 'ct_collector_publish_facts']) {
    const result = await rpc(method, { agent_id: agent }, undefined, 404);
    if (result.value.error?.code !== 'not_found') throw Error(`${method}: expected retired not_found response`);
    report.results.push({ scenario: `${method}_retired`, status: 404, error: 'not_found', wall_ms: result.wall_ms });
  }
  for (const method of ['ct_connection_status', 'ct_workspace_snapshot']) {
    const samples_ms = [];
    for (let index = 0; index < 20; index++) samples_ms.push((await rpc(method)).wall_ms);
    const sorted = [...samples_ms].sort((a, b) => a - b);
    report.results.push({ scenario: method, requests: samples_ms.length, samples_ms,
      wall_p50_ms: sorted[Math.ceil(sorted.length * 0.5) - 1],
      wall_p95_ms: sorted[Math.ceil(sorted.length * 0.95) - 1] });
  }
  report.http_requests = requestCount;
  writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: 'ok', scope: report.scope, results: report.results.map(row => row.scenario) }));
} finally {
  await mf.dispose();
}
