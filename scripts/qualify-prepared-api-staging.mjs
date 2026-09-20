#!/usr/bin/env node
/** Exercise the exact staging driver against disposable local Miniflare. */
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]`
  : value !== null && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
    : JSON.stringify(value);
const plan = process.argv[2];
if (!plan) throw Error('usage: qualify-prepared-api-staging.mjs PLAN');

const workspace = '00000000-0000-4000-8000-000000009401';
const agent = '00000000-0000-4000-8000-000000009402';
const version = '00000000-0000-4000-8000-000000009403';
const token = 'prepared-api-staging-local-token-0000000001';
const principals = { [sha(token)]: { workspace_id: workspace, agent_id: agent, roles: ['read', 'collect'] } };
const bundle = await build({ entryPoints: [`${root}cloudflare/control-plane/src/index.ts`], bundle: true,
  write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ name: 'prepared-api-staging-local',
  modules: true, script: bundle.outputFiles[0].text, compatibilityDate: '2026-09-10',
  durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } }, r2Buckets: ['ARTIFACTS'],
  bindings: { CT_CURSOR_KEY: 'prepared-api-staging-local-cursor-key-00001',
    CT_PRINCIPALS: JSON.stringify(principals), WORKER_VERSION: { id: version } } }] }));
const directory = mkdtempSync(join(tmpdir(), 'ct-prepared-staging-'));
const generatedPlan = join(directory, 'plan.json');
const representativePlan = join(directory, 'representative-plan.json');
const publication = join(directory, 'publication.json');
const partial = join(directory, 'partial.json');
const recovery = join(directory, 'recovery.json');
const preflight = join(directory, 'preflight.json');
const full = join(directory, 'full.json');
try {
  const address = await mf.ready;
  const env = { ...process.env, CT_STAGING_URL: address.origin, CT_STAGING_ALLOW_HTTP: '1',
    CT_STAGING_PUBLICATION_TOKEN: token, CT_STAGING_WORKSPACE_ID: workspace,
    CT_STAGING_AGENT_ID: agent, CT_STAGING_EXPECTED_VERSION: version };
  const invoke = args => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [`${root}scripts/run-prepared-api-staging.mjs`, ...args],
      { cwd: root, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve() : reject(Error(`driver exited ${code}`)));
  });
  const supplied = JSON.parse(readFileSync(plan));
  const fixturePaths = supplied.fixtures.map(row =>
    `${root}.amp/ct-api-staging/fixtures/${row.shape}.json`);
  await invoke(['plan', ...fixturePaths, generatedPlan]);
  const generated = JSON.parse(readFileSync(generatedPlan));
  const representative = structuredClone(generated);
  representative.fixtures = representative.fixtures.filter(row => row.shape === 'representative');
  representative.publication.total_http_requests = representative.fixtures[0].publication_http_requests;
  representative.publication.total_object_puts = representative.fixtures[0].object_puts;
  representative.publication.total_object_put_bytes = representative.fixtures[0].object_put_bytes;
  writeFileSync(representativePlan, JSON.stringify(representative, null, 2) + '\n');
  await invoke(['publish', representativePlan, publication]);
  const representativeResult = JSON.parse(readFileSync(publication));
  const near = generated.fixtures.find(row => row.shape === 'near-budget');
  const nearFixture = JSON.parse(readFileSync(`${root}${near.fixture_path}`));
  let sequence = 0;
  const rpc = async (method, params) => {
    const id = `local-resume-${sequence++}`;
    const response = await fetch(new URL('/v1/core', address), { method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ protocol: 'ct.core.v1', id, method,
        params: { workspace_id: workspace, ...params } }) });
    const body = await response.json();
    if (response.status !== 200 || body.ok !== true) throw Error(`${method}: local seed failed`);
    return body.data;
  };
  const project = await rpc('ct_project_register', { agent_id: agent,
    display_name: `CT API staging near-budget ${nearFixture.root.slice(0, 8)}` });
  const source = await rpc('ct_collector_register_source', { agent_id: agent,
    project_id: project.project_id, vendor: 'pi', native_session_id: nearFixture.root });
  const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] },
    session_digest: nearFixture.source };
  const content = sha(stable(payload));
  await rpc('ct_collector_publish_observation', { agent_id: agent, ...source,
    source_sequence: 0, event_id: `checkpoint:${content}`, parser_version: 'staging.v1',
    content_sha256: content, observed_at: '2026-09-19T00:00:00Z', payload });
  const facts = near.objects.find(row => row.kind === 'facts');
  const prior = { ...representativeResult, status: 'FAIL', http_requests: 70,
    object_puts: 62, object_put_bytes: 220426,
    requests: [...representativeResult.requests,
      { request_id: 'near-project', method: 'ct_project_register', outcome: 'success', http_status: 200 },
      { request_id: 'near-source', method: 'ct_collector_register_source', outcome: 'success', http_status: 200 },
      { request_id: 'near-observation', method: 'ct_collector_publish_observation', outcome: 'success', http_status: 200 },
      { request_id: 'near-facts', method: 'PUT artifact/facts', outcome: 'unknown', http_status: null,
        sha256: facts.sha256, bytes: facts.bytes, failure: 'TimeoutError' }] };
  writeFileSync(partial, JSON.stringify(prior, null, 2) + '\n');
  const recoveryEvidence = { shape: near.shape, fixture_sha256: near.fixture_sha256,
    plan_sha256: sha(readFileSync(generatedPlan)), prior_publication_sha256: sha(readFileSync(partial)),
    project_id: project.project_id, source_id: source.source_id, source_epoch: source.source_epoch,
    facts: { outcome: 'object_not_found', sha256: facts.sha256, bytes: facts.bytes } };
  writeFileSync(recovery, JSON.stringify(recoveryEvidence, null, 2) + '\n');
  for (const args of [
    ['resume', generatedPlan, partial, recovery, publication],
    ['measure', generatedPlan, publication, preflight, '--preflight'],
    ['measure', generatedPlan, publication, full, '--full'],
  ]) {
    await invoke(args);
  }
  const publicationResult = JSON.parse(readFileSync(publication));
  const preflightResult = JSON.parse(readFileSync(preflight));
  const fullResult = JSON.parse(readFileSync(full));
  if (publicationResult.status !== 'PASS' || preflightResult.status !== 'PASS'
      || fullResult.status !== 'PASS' || preflightResult.http_requests !== 8
      || fullResult.http_requests !== 602 || publicationResult.http_requests !== 1008
      || publicationResult.object_puts !== 1007 || publicationResult.combined_http_requests !== 1078)
    throw Error('staging resume driver qualification mismatch');
  console.log(`PASS receipt resume: successor=${publicationResult.http_requests} HTTP/${publicationResult.object_puts} PUT, combined=1078 HTTP, preflight=8, full=602`);
} finally {
  await mf.dispose();
  rmSync(directory, { recursive: true, force: true });
}
