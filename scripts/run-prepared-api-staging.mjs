#!/usr/bin/env node
/** Bounded staging publication and performance driver. No retries. */
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]`
  : value !== null && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
    : JSON.stringify(value);
const requireThat = (condition, message) => { if (!condition) throw Error(message); };
const save = (path, value) => {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + '\n');
};

function fixtureRecord(path) {
  const bytes = readFileSync(path);
  const fixture = JSON.parse(bytes);
  const benchmark = fixture.benchmark;
  requireThat(['representative', 'near-budget'].includes(benchmark?.shape), 'fixture shape');
  const expectedTurns = benchmark.shape === 'representative' ? 3 : 66;
  const totalTurns = benchmark.shape === 'representative' ? 3 : 97;
  const expectedCommands = benchmark.shape === 'representative' ? 24 : 776;
  requireThat(benchmark.expected_data.turns.length === expectedTurns, 'returned turn count');
  requireThat(benchmark.expected_data.page.total === totalTurns, 'total turn count');
  requireThat(fixture.cwd.length === (benchmark.shape === 'representative' ? 14 : 120006), 'cwd shape');
  requireThat(fixture.facts.rows.filter(row => row.kind === 'item').length === expectedCommands,
    'command count');
  const bodies = [
    { kind: 'facts', body: stable(fixture.facts) },
    { kind: 'summary', body: stable(fixture.summary) },
    ...Object.entries(fixture.api.objects).map(([digest, body]) => {
      requireThat(sha(body) === digest, `API object digest ${digest}`);
      return { kind: 'api', body };
    }),
  ].map(row => ({ ...row, sha256: sha(row.body), bytes: Buffer.byteLength(row.body) }));
  return {
    fixture,
    manifest: {
      shape: benchmark.shape,
      fixture_path: relative(process.cwd(), path),
      fixture_sha256: sha(bytes),
      fixture_bytes: bytes.length,
      root_session_id: fixture.root,
      fact_set_digest: fixture.source,
      turns_total: totalTurns,
      turns_returned: expectedTurns,
      commands: expectedCommands,
      cwd_characters: fixture.cwd.length,
      expected_r2_reads: benchmark.expected_reads,
      expected_r2_body_bytes: benchmark.expected_fetched_bytes,
      expected_response_data_sha256: sha(stable(benchmark.expected_data)),
      object_puts: bodies.length,
      object_put_bytes: bodies.reduce((sum, row) => sum + row.bytes, 0),
      publication_http_requests: bodies.length + 4,
      objects: bodies.map(({ kind, sha256, bytes }) => ({ kind, sha256, bytes })),
    },
    bodies,
  };
}

function createPlan(fixturePaths) {
  const records = fixturePaths.map(path => fixtureRecord(resolve(path)));
  requireThat(new Set(records.map(row => row.manifest.shape)).size === 2, 'two distinct shapes required');
  const preflightPerShape = 4;
  const fullPerShape = 1 + 100 + 25 * 8;
  return {
    schema_version: 'ct.prepared-api-staging-plan.v1',
    source_head: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
    source_tree: execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { encoding: 'utf8' }).trim(),
    created_at: new Date().toISOString(),
    fixtures: records.map(row => row.manifest),
    publication: {
      automatic_retries: 0,
      total_http_requests: records.reduce((sum, row) => sum + row.manifest.publication_http_requests, 0),
      total_object_puts: records.reduce((sum, row) => sum + row.manifest.object_puts, 0),
      total_object_put_bytes: records.reduce((sum, row) => sum + row.manifest.object_put_bytes, 0),
    },
    preflight: {
      requests_per_shape: preflightPerShape,
      total_requests: preflightPerShape * records.length,
      estimated_r2_reads: records.reduce((sum, row) => sum + preflightPerShape * row.manifest.expected_r2_reads, 0),
      estimated_r2_body_bytes: records.reduce((sum, row) => sum + preflightPerShape * row.manifest.expected_r2_body_bytes, 0),
    },
    full: {
      first_read_per_shape: 1,
      sequential_per_shape: 100,
      concurrent_batches_per_shape: 25,
      concurrency: 8,
      requests_per_shape: fullPerShape,
      total_requests: fullPerShape * records.length,
      estimated_r2_reads: records.reduce((sum, row) => sum + fullPerShape * row.manifest.expected_r2_reads, 0),
      estimated_r2_body_bytes: records.reduce((sum, row) => sum + fullPerShape * row.manifest.expected_r2_body_bytes, 0),
      automatic_retries: 0,
    },
  };
}

function runtime() {
  const base = new URL(process.env.CT_STAGING_URL ?? '');
  const token = process.env.CT_STAGING_PUBLICATION_TOKEN ?? '';
  const workspaceId = process.env.CT_STAGING_WORKSPACE_ID ?? '';
  const agentId = process.env.CT_STAGING_AGENT_ID ?? '';
  const version = process.env.CT_STAGING_EXPECTED_VERSION ?? '';
  const localQualification = process.env.CT_STAGING_ALLOW_HTTP === '1'
    && base.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(base.hostname);
  requireThat(base.protocol === 'https:' || localQualification,
    'CT_STAGING_URL must use HTTPS outside local qualification');
  requireThat(/^[A-Za-z0-9_-]{32,256}$/.test(token), 'protected staging token required');
  requireThat(/^[0-9a-f-]{36}$/.test(workspaceId) && /^[0-9a-f-]{36}$/.test(agentId),
    'staging principal identifiers required');
  requireThat(/^[0-9a-f-]{36}$/.test(version), 'expected deployed version required');
  return { base, token, workspaceId, agentId, version };
}

async function checkedFetch(run, report, path, init, expectedStatus, evidence, timeoutMs = 30_000) {
  report.http_requests++;
  if (evidence) report.requests.push(evidence);
  save(report.output, report);
  let response;
  try {
    response = await fetch(new URL(path, run.base), {
      ...init, redirect: 'error', signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    if (evidence) Object.assign(evidence, { outcome: 'unknown', failure: error?.name ?? 'transport_error' });
    save(report.output, report);
    throw error;
  }
  const body = await response.text();
  if (evidence) Object.assign(evidence, { http_status: response.status,
    worker_version: response.headers.get('x-ct-worker-version') });
  requireThat(response.headers.get('x-ct-worker-version') === run.version,
    `${path}: deployed version drift`);
  if (response.status !== expectedStatus) {
    let code = 'non_json_error';
    try { code = JSON.parse(body)?.error?.code ?? code; } catch { /* sanitized below */ }
    if (evidence) Object.assign(evidence, { outcome: 'failure', failure: code });
    save(report.output, report);
    throw Error(`${path}: HTTP ${response.status} ${code}`);
  }
  return { response, body };
}

function auth(run, extra = {}) {
  return { authorization: `Bearer ${run.token}`, ...extra };
}

async function publish(planPath, output) {
  const plan = JSON.parse(readFileSync(planPath));
  const run = runtime();
  requireThat(plan.source_head === execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
    'plan source head drift');
  const report = { schema_version: 'ct.prepared-api-staging-publication.v1', output,
    source_head: plan.source_head, source_tree: plan.source_tree, started_at: new Date().toISOString(),
    completed_at: null, status: 'running', http_requests: 0, object_puts: 0,
    object_put_bytes: 0, requests: [], fixtures: [], failure: null };
  save(output, report);
  async function rpc(method, params, requestId) {
    const evidence = { request_id: requestId, method, outcome: 'issued', http_status: null };
    const { body } = await checkedFetch(run, report, '/v1/core', { method: 'POST',
      headers: auth(run, { 'content-type': 'application/json', 'x-ct-stage-request-id': requestId }),
      body: JSON.stringify({ protocol: 'ct.core.v1', id: requestId, method,
        params: { workspace_id: run.workspaceId, ...params } }) }, 200, evidence);
    const parsed = JSON.parse(body);
    requireThat(parsed.ok === true && parsed.id === requestId, `${method}: response envelope`);
    evidence.outcome = 'success'; save(output, report); return parsed.data;
  }
  try {
    for (const descriptor of plan.fixtures) {
      const record = fixtureRecord(resolve(descriptor.fixture_path));
      requireThat(record.manifest.fixture_sha256 === descriptor.fixture_sha256, 'fixture drift');
      const fixture = record.fixture, shape = descriptor.shape;
      const prefix = `publish-${shape}`;
      const project = await rpc('ct_project_register', { agent_id: run.agentId,
        display_name: `CT API staging ${shape} ${fixture.root.slice(0, 8)}` }, `${prefix}-project-${randomUUID()}`);
      const source = await rpc('ct_collector_register_source', { agent_id: run.agentId,
        project_id: project.project_id, vendor: 'pi', native_session_id: fixture.root },
      `${prefix}-source-${randomUUID()}`);
      const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] },
        session_digest: fixture.source };
      const content = sha(stable(payload));
      await rpc('ct_collector_publish_observation', { agent_id: run.agentId, ...source,
        source_sequence: 0, event_id: `checkpoint:${content}`, parser_version: 'staging.v1',
        content_sha256: content, observed_at: '2026-09-19T00:00:00Z', payload },
      `${prefix}-observation-${randomUUID()}`);
      const refs = [];
      for (const object of record.bodies) {
        const requestId = `${prefix}-put-${object.kind}-${object.sha256.slice(0, 12)}-${randomUUID()}`;
        const evidence = { request_id: requestId, method: `PUT artifact/${object.kind}`,
          outcome: 'issued', http_status: null, sha256: object.sha256, bytes: object.bytes };
        await checkedFetch(run, report, `/v1/artifacts/${object.kind}/${object.sha256}`, {
          method: 'PUT', headers: auth(run, { 'content-type': 'application/json',
            'x-ct-stage-request-id': requestId }), body: object.body }, 200, evidence, 120_000);
        evidence.outcome = 'success'; report.object_puts++; report.object_put_bytes += object.bytes;
        refs.push({ kind: object.kind, sha256: object.sha256, bytes: object.bytes }); save(output, report);
      }
      const byKind = kind => refs.find(row => row.kind === kind);
      const publication = { agent_id: run.agentId, project_id: project.project_id,
        publication_sequence: 0, source_vector: [{ ...source, source_sequence: 0, content_sha256: content }],
        graphs: [{ graph_id: fixture.root, graph_input_sha256: fixture.source,
          fact_set_digest: fixture.source, fact_count: fixture.facts.rows.length,
          source_ids: [source.source_id], vendors: ['pi'], observed_at: '2026-09-19T00:00:00Z',
          facts: byKind('facts'), summary: byKind('summary'), api_methods: fixture.api.methods,
          api_objects: refs.filter(row => row.kind === 'api') }] };
      const published = await rpc('ct_collector_publish_artifacts', publication,
        `${prefix}-manifest-${randomUUID()}`);
      report.fixtures.push({ shape, fixture_sha256: descriptor.fixture_sha256,
        root_session_id: fixture.root, project_id: project.project_id, source_id: source.source_id,
        publication_result: published, object_puts: record.manifest.object_puts,
        object_put_bytes: record.manifest.object_put_bytes });
      save(output, report);
    }
    requireThat(report.http_requests === plan.publication.total_http_requests, 'publication request count');
    requireThat(report.object_puts === plan.publication.total_object_puts, 'publication PUT count');
    requireThat(report.object_put_bytes === plan.publication.total_object_put_bytes, 'publication byte count');
    report.status = 'PASS';
  } catch (error) {
    report.status = 'FAIL'; report.failure = error instanceof Error ? error.message : String(error); throw error;
  } finally { report.completed_at = new Date().toISOString(); save(output, report); }
}

function normalizedData(data, expected) {
  const result = structuredClone(data);
  result.project.project_id = expected.project.project_id;
  result.page.next_cursor = expected.page.next_cursor;
  return result;
}

async function measure(planPath, publicationPath, output, mode) {
  const plan = JSON.parse(readFileSync(planPath));
  const publication = JSON.parse(readFileSync(publicationPath));
  const run = runtime();
  requireThat(publication.status === 'PASS', 'publication incomplete');
  const limits = plan[mode];
  const report = { schema_version: 'ct.prepared-api-staging-http-evidence.v1', output,
    mode, run_id: `ct-api-staging-${mode}-${randomUUID()}`, source_head: plan.source_head,
    source_tree: plan.source_tree, deployed_version: run.version, started_at: new Date().toISOString(),
    completed_at: null, status: 'running', http_requests: 0, estimated_r2_reads: 0,
    estimated_r2_body_bytes: 0, requests: [], failure: null };
  save(output, report);
  async function one(descriptor, phase, sequence) {
    const fixture = fixtureRecord(resolve(descriptor.fixture_path)).fixture;
    const published = publication.fixtures.find(row => row.shape === descriptor.shape);
    const requestId = `${descriptor.shape}-${phase}-${sequence}-${randomUUID()}`;
    const evidence = { request_id: requestId, shape: descriptor.shape, phase, sequence,
      outcome: 'issued', http_status: null, worker_version: null, elapsed_ms: null,
      response_bytes: null, expected_r2_reads: descriptor.expected_r2_reads,
      expected_r2_body_bytes: descriptor.expected_r2_body_bytes };
    report.estimated_r2_reads += descriptor.expected_r2_reads;
    report.estimated_r2_body_bytes += descriptor.expected_r2_body_bytes;
    const started = performance.now();
    try {
      const { body } = await checkedFetch(run, report, '/v1/api', { method: 'POST',
        headers: auth(run, { 'content-type': 'application/json', 'x-ct-stage-run-id': report.run_id,
          'x-ct-stage-request-id': requestId, 'x-ct-stage-shape': descriptor.shape,
          'x-ct-stage-phase': phase }),
        body: JSON.stringify({ protocol: 'ct.api.v1', id: requestId, method: 'session.overview',
          method_version: fixture.versions['session.overview'],
          params: { session_id: fixture.root, limit: 200 } }) }, 200, evidence);
      const parsed = JSON.parse(body);
      requireThat(parsed.ok === true && parsed.id === requestId, `${requestId}: envelope`);
      requireThat(parsed.data.project.project_id === published.project_id, `${requestId}: project identity`);
      assert.deepEqual(normalizedData(parsed.data, fixture.benchmark.expected_data),
        fixture.benchmark.expected_data, `${requestId}: offline parity`);
      Object.assign(evidence, { outcome: 'success', elapsed_ms: performance.now() - started,
        response_bytes: Buffer.byteLength(body), view_manifest_sha256: parsed.meta.identity.view_manifest_sha256 });
    } catch (error) {
      evidence.elapsed_ms = performance.now() - started;
      if (evidence.outcome === 'issued') evidence.outcome = 'failure';
      if (!evidence.failure) evidence.failure = error instanceof Error ? error.message : String(error);
      if (!report.failure) report.failure = { request_id: requestId, outcome: evidence.outcome,
        message: evidence.failure };
      throw error;
    } finally { save(output, report); }
  }
  try {
    for (const descriptor of plan.fixtures) {
      if (mode === 'preflight') {
        for (let n = 0; n < limits.requests_per_shape; n++) await one(descriptor, 'trace-preflight', n);
        continue;
      }
      await one(descriptor, 'first-read-after-publication', 0);
      for (let n = 0; n < limits.sequential_per_shape; n++) await one(descriptor, 'sequential', n);
      for (let batch = 0; batch < limits.concurrent_batches_per_shape; batch++) {
        const issued = Array.from({ length: limits.concurrency }, (_value, index) =>
          one(descriptor, 'concurrent', batch * limits.concurrency + index));
        const settled = await Promise.allSettled(issued);
        const failed = settled.find(row => row.status === 'rejected');
        if (failed) throw failed.reason;
      }
    }
    requireThat(report.http_requests === limits.total_requests, 'measurement request count');
    requireThat(report.estimated_r2_reads === limits.estimated_r2_reads, 'R2 read estimate');
    requireThat(report.estimated_r2_body_bytes === limits.estimated_r2_body_bytes, 'R2 byte estimate');
    requireThat(report.requests.every(row => row.outcome === 'success'), 'incomplete measurements');
    report.status = 'PASS';
  } catch (error) {
    report.status = 'FAIL'; if (!report.failure) report.failure = { request_id: null,
      outcome: 'failure', message: error instanceof Error ? error.message : String(error) }; throw error;
  } finally { report.completed_at = new Date().toISOString(); save(output, report); }
}

const [command, ...args] = process.argv.slice(2);
if (command === 'plan') {
  requireThat(args.length === 3, 'usage: plan FIXTURE_A FIXTURE_B OUTPUT');
  save(resolve(args[2]), createPlan(args.slice(0, 2).map(value => resolve(value))));
} else if (command === 'publish') {
  requireThat(args.length === 2, 'usage: publish PLAN OUTPUT');
  await publish(resolve(args[0]), resolve(args[1]));
} else if (command === 'measure') {
  const mode = args.includes('--preflight') ? 'preflight' : args.includes('--full') ? 'full' : null;
  const positional = args.filter(value => !value.startsWith('--'));
  requireThat(mode && positional.length === 3, 'usage: measure PLAN PUBLICATION OUTPUT (--preflight|--full)');
  await measure(...positional.map(value => resolve(value)), mode);
} else {
  throw Error('usage: run-prepared-api-staging.mjs plan|publish|measure ...');
}
