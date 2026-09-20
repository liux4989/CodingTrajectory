// Disposable Miniflare integration. Never loads Wrangler credentials or remote bindings.
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const codecBundle = await build({ entryPoints: [`${root}cloudflare/control-plane/src/artifact-manifest.ts`], bundle: true, write: false, format: 'esm', platform: 'node' });
const { compactGraph, expandGraph } = await import(`data:text/javascript;base64,${Buffer.from(codecBundle.outputFiles[0].text).toString('base64')}`);
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const lastOrdinal = fixture.benchmark?.shape === 'representative' ? 2 : 96;
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value !== null && typeof value === 'object'
  ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}` : JSON.stringify(value);
const workspace = '00000000-0000-0000-0000-000000000001', agent = '00000000-0000-0000-0000-000000000002';
const tokens = { owner: 'direct-api-local-owner-token-00000001', read: 'direct-api-local-reader-token-0000001', collect: 'direct-api-local-collect-token-000001', other: 'direct-api-local-other-token-00000001' };
const principals = Object.fromEntries(Object.entries(tokens).map(([role, token]) => [sha(token), {
  workspace_id: role === 'other' ? '00000000-0000-0000-0000-000000000009' : workspace, agent_id: agent,
  roles: ['collect', 'read'].includes(role) ? [role] : ['owner'],
}]));
const publicationBenchmark = ['--publication', '--publication-timing', '--publication-baseline'].includes(process.argv[3]);
const qualifyPublication = process.argv[3] === '--publication';
const baseline = process.argv[3] === '--publication-baseline';
const delayMs = Number(process.argv[5] ?? 0);
const inputGate = process.argv[6] === '--input-gate';
const bundle = await build({ entryPoints: [`${root}${publicationBenchmark ? 'scripts/artifact-benchmark-worker.ts' : 'cloudflare/control-plane/src/index.ts'}`], bundle: true, write: false,
  format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const benchmarking = process.argv[3] === '--benchmark';
const mf = new Miniflare(convertV4MiniflareOptions({ ...(benchmarking ? { inspectorPort: 0 } : {}), workers: [{ name: 'direct-api', modules: true, script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-09-10', durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } },
  r2Buckets: ['ARTIFACTS'], bindings: { CT_CURSOR_KEY: 'direct-api-local-cursor-key-00000000001', CT_PRINCIPALS: JSON.stringify(principals), LOCAL_PUBLICATION_INPUT_GATE: inputGate },
}] }));
async function post(path, message, role = 'owner', expected = 200) {
  const response = await mf.dispatchFetch(`http://local${path}`, { method: 'POST', headers: { authorization: `Bearer ${tokens[role] ?? 'invalid'}` }, body: JSON.stringify(message) });
  const raw = await response.text();
  assert.equal(response.status, expected, raw.slice(0, 1000));
  if (path === '/v1/api') {
    assert.ok(Buffer.byteLength(raw) <= 448 * 1024);
    assert.equal(response.headers.get('content-type'), 'application/json');
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
  return JSON.parse(raw);
}
async function rpc(method, params, key) {
  if (method === 'ct_collector_publish_artifacts') params = { ...params, schema_version: 'ct.artifact-manifest.v3', graphs: params.graphs.map(compactGraph) };
  return (await post('/v1/core', { protocol: 'ct.core.v1', method, params: { workspace_id: workspace, ...params }, ...(key ? { idempotency_key: key } : {}) })).data;
}
async function api(method, params = {}, role = 'owner', expected = 200, version = fixture.versions[method] ?? 5) {
  return post('/v1/api', { protocol: 'ct.api.v1', method, method_version: version, params }, role, expected);
}
async function upload(kind, body) {
  const hash = sha(body);
  const response = await mf.dispatchFetch(`http://local/v1/artifacts/${kind}/${hash}`, { method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body });
  assert.equal(response.status, 200, await response.text());
  return { kind, sha256: hash, bytes: Buffer.byteLength(body) };
}
const local = async (path, value) => (await mf.dispatchFetch(`http://local/__benchmark/${path}`, {
  method: 'POST', body: JSON.stringify(value),
})).json();
const claimProbe = (ref, action, completion) => local('claim-probe', { ...ref, action, completion });
const internal = (method, ref) => local('internal', { method: `ct_internal_artifact_${method}`, request: { workspace_id: workspace, ...ref } });
const objectKey = ref => `workspaces/${workspace}/artifacts/${ref.kind}/${ref.sha256}`;
const readiness = async objects => (await rpc('ct_collector_artifact_readiness', { agent_id: agent, objects })).ready;
async function waitGate(name) {
  for (let attempt = 0; attempt < 1000; attempt++) {
    if ((await (await mf.dispatchFetch(`http://local/__benchmark/${name}`)).json()).entered) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw Error(`${name} did not enter`);
}
const invocationCounts = async () => (await mf.dispatchFetch('http://local/__benchmark/invocations')).json();
async function waitInvocation(method, count) {
  for (let attempt = 0; attempt < 1000; attempt++) {
    if ((await invocationCounts())[method] >= count) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw Error(`${method} did not arrive`);
}
async function rejectPublication(publication, code, status = 409) {
  const response = await post('/v1/core', { protocol: 'ct.core.v1', method: 'ct_collector_publish_artifacts',
    params: { workspace_id: workspace, ...publication } }, 'owner', status);
  assert.equal(response.error.code, code);
}
async function qualifyReceiptsBefore(publication) {
  const graph = publication.graphs[0], ref = graph.summary;
  assert.equal((await internal('reject', ref)).rejected, true);
  assert.equal((await internal('claim', ref)).status, 200);
  const bucket = await mf.getR2Bucket('ARTIFACTS');
  const body = stable(fixture.summary), key = objectKey(ref);
  const metadata = { workspace_id: workspace, kind: ref.kind, sha256: ref.sha256 };
  const original = (await claimProbe(ref))[0];
  assert.ok(original.completion);
  assert.deepEqual(await readiness([ref, { ...ref, bytes: ref.bytes + 1 }, { ...ref, kind: 'facts' }]), [true, false, false]);
  assert.deepEqual((await claimProbe(ref))[0], original); // Readiness never renews a claim.
  const envelope = { protocol: 'ct.core.v1', method: 'ct_collector_artifact_readiness', params: { workspace_id: workspace, agent_id: agent, objects: [ref] } };
  await post('/v1/core', envelope, 'read', 403);
  await post('/v1/core', envelope, 'other', 403);
  assert.deepEqual((await post('/v1/core', envelope, 'collect')).data.ready, [true]);
  assert.deepEqual((await post('/v1/core', { ...envelope, params: { ...envelope.params, workspace_id: '00000000-0000-0000-0000-000000000009' } }, 'other')).data.ready, [false]);
  await post('/v1/core', { ...envelope, params: { ...envelope.params, objects: Array(513).fill(ref) } }, 'owner', 400);
  const batch = Array.from({ length: 512 }, (_, n) => n % 2 ? ref : { ...ref, sha256: n.toString(16).padStart(64, '0') });
  const r2Before = await (await mf.dispatchFetch('http://local/__benchmark/r2')).json();
  const checked = await rpc('ct_collector_artifact_readiness', { agent_id: agent, objects: batch });
  assert.deepEqual(checked.ready, batch.map((_, n) => Boolean(n % 2)));
  assert.ok(Object.values(checked.__benchmark.groups).every(group => group.rowsWritten === 0));
  assert.deepEqual(await (await mf.dispatchFetch('http://local/__benchmark/r2')).json(), r2Before);
  const indexRef = graph.api_methods.find(method => method.index).index;
  const indexClaim = (await claimProbe(indexRef))[0];
  await claimProbe(indexRef, 'completion', { ...JSON.parse(indexClaim.completion), index: null });
  assert.deepEqual(await readiness([indexRef, { ...indexRef, requires_index: true }]), [true, false]);
  await claimProbe(indexRef, 'completion', JSON.parse(indexClaim.completion));
  assert.deepEqual(await readiness([{ ...indexRef, requires_index: true }]), [true]);
  // An R2 object alone, or a pending/expired claim, is not readiness.
  await claimProbe(ref, 'delete');
  assert.deepEqual(await readiness([ref]), [false]);
  await rejectPublication(publication, 'artifact_upload_incomplete');
  const pending = await internal('claim', ref);
  assert.deepEqual(await readiness([ref]), [false]);
  await rejectPublication(publication, 'artifact_upload_incomplete');
  for (const customMetadata of [{ ...metadata, workspace_id: 'other' }, { ...metadata, kind: 'facts' }, { ...metadata, sha256: 'f'.repeat(64) }]) {
    await bucket.put(key, body, { customMetadata });
    const response = await mf.dispatchFetch(`http://local/v1/artifacts/summary/${ref.sha256}`, {
      method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body });
    assert.equal(response.status, 409);
    assert.equal((await claimProbe(ref))[0].completion, null);
  }
  await bucket.put(key, body.replace('ct.prepared-summary', 'ct.prepared-summarX'), { customMetadata: metadata });
  const corrupt = await mf.dispatchFetch(`http://local/v1/artifacts/summary/${ref.sha256}`, {
    method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body });
  assert.equal(corrupt.status, 409); // Same size/metadata, different bytes.
  await bucket.put(key, body, { customMetadata: metadata });
  await mf.dispatchFetch('http://local/__benchmark/r2?reset');
  await upload('summary', body);
  assert.equal((await (await mf.dispatchFetch('http://local/__benchmark/r2')).json()).calls.put ?? 0, 0);
  const completed = (await claimProbe(ref))[0];
  assert.equal(completed.token, pending.body.token);
  await internal('claim', ref);
  assert.equal((await claimProbe(ref))[0].completion, completed.completion);
  for (const completion of [{ ...JSON.parse(completed.completion), bytes: ref.bytes + 1 }, { ...JSON.parse(completed.completion), workspace_id: 'other' }]) {
    await claimProbe(ref, 'completion', completion);
    assert.deepEqual(await readiness([ref]), [false]);
    await rejectPublication(publication, 'artifact_upload_incomplete');
  }
  await claimProbe(ref, 'completion', JSON.parse(completed.completion));
  await local('claim-expiry', { ...ref, expiresAt: 0 });
  assert.deepEqual(await readiness([ref]), [false]);
  await rejectPublication(publication, 'artifact_upload_incomplete');
  const renewed = await internal('claim', ref);
  assert.notEqual(renewed.body.token, completed.token);
  assert.equal((await internal('complete', { ...ref, token: completed.token, index: null })).status, 409);
  assert.equal((await claimProbe(ref))[0].completion, null);
  await upload('summary', body);
  // Manifest-dependent invariants still belong at publication.
  const changed = mutate => { const copy = structuredClone(publication); mutate(copy); return copy; };
  await rejectPublication(changed(p => p.source_vector[0].source_sequence++), 'source_vector_requires_accepted_project_checkpoints', 400);
  await rejectPublication(changed(p => p.graphs[0].api_methods.push(p.graphs[0].api_methods[0])), 'invalid_prepared_method', 400);
  await rejectPublication(changed(p => p.graphs[0].api_methods.find(m => m.index).scope = 'different'), 'invalid_prepared_reference', 400);
  await rejectPublication(changed(p => p.graphs[0].fact_set_digest = 'f'.repeat(64)), 'invalid_prepared_reference', 400);
  const indexMethod = graph.api_methods.find(m => m.index && JSON.parse(fixture.api.objects[m.index.sha256]).mode === 'page');
  const index = JSON.parse(fixture.api.objects[indexMethod.index.sha256]);
  await rejectPublication(changed(p => p.graphs[0].api_objects = p.graphs[0].api_objects.filter(r => r.sha256 !== index.topology.sha256)), 'invalid_prepared_reference', 400);
  await rejectPublication(changed(p => p.graphs[0].facts.bytes++), 'artifact_upload_incomplete');
  for (const mutate of [i => i.mode = 'unknown', i => i.sizes.pop(), i => i.packs[0].start = 1,
    i => i.packs.at(-1).end--, i => i.postings = { id: { bad: [i.total] } }, i => i.topology.bytes = 128 * 1024 + 1]) {
    const malformed = structuredClone(index); mutate(malformed);
    const bytes = stable(malformed), hash = sha(bytes);
    const response = await mf.dispatchFetch(`http://local/v1/artifacts/api/${hash}`, {
      method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body: bytes });
    assert.equal(response.status, 400, await response.text());
    assert.deepEqual(await claimProbe({ kind: 'api', sha256: hash }), []);
  }
  const columnIndex = graph.api_methods.map(m => m.index && JSON.parse(fixture.api.objects[m.index.sha256])).find(i => i?.mode === 'page_columns');
  if (columnIndex) {
    const ref = Object.values(columnIndex.posting_objects)[0];
    await rejectPublication(changed(p => p.graphs[0].api_objects = p.graphs[0].api_objects.filter(r => r.sha256 !== ref.sha256)), 'invalid_prepared_reference', 400);
    const column = JSON.parse(fixture.api.objects[ref.sha256]);
    for (const value of [{ ...column, posting: 'unknown' }, { ...column, values: { invalid: [column.total] } },
      { ...column, values: { invalid: [1, 0] } }, { ...columnIndex, posting_objects: { unknown: ref } }]) {
      const body = stable(value), hash = sha(body);
      const response = await mf.dispatchFetch(`http://local/v1/artifacts/api/${hash}`, {
        method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body });
      assert.equal(response.status, 400, await response.text());
      assert.deepEqual(await claimProbe({ kind: 'api', sha256: hash }), []);
    }
  }
  // A failed PUT leaves a pending fence, never a completion.
  const failedBody = stable({ ...fixture.facts, qualification: 'failed-put' });
  const failedRef = { kind: 'facts', sha256: sha(failedBody), bytes: Buffer.byteLength(failedBody) };
  await local('object-gate', { operation: 'put', key: objectKey(failedRef), fail: true });
  const failing = mf.dispatchFetch(`http://local/v1/artifacts/facts/${failedRef.sha256}`, {
    method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body: failedBody });
  await waitGate('object-gate');
  await rejectPublication(changed(p => p.graphs[0].facts = failedRef), 'artifact_upload_incomplete');
  await mf.dispatchFetch('http://local/__benchmark/object-gate', { method: 'DELETE' });
  const failure = await failing;
  assert.equal(failure.status, 503);
  assert.equal((await failure.json()).error.code, 'authority_unavailable');
  assert.equal((await claimProbe(failedRef))[0].completion, null);
  await claimProbe(failedRef, 'delete');
  console.log('PASS upload receipts: missing/pending/expired, existing immutable integrity and metadata, generation fencing, malformed indexes, manifest source/identity/dependency/byte checks, failed PUT');
}

async function qualifyReceiptsAfter(publication, published) {
  const ref = publication.graphs[0].summary;
  assert.deepEqual(await claimProbe(ref), []);
  const indexRef = publication.graphs[0].api_methods.find(method => method.index).index;
  assert.deepEqual(await readiness([ref, { ...indexRef, requires_index: true }]), [true, true]);
  const priorFacts = await claimProbe(indexRef, 'index-facts', null);
  assert.deepEqual(await readiness([indexRef, { ...indexRef, requires_index: true }]), [true, false]);
  await claimProbe(indexRef, 'index-facts', priorFacts[0]);
  assert.deepEqual(await readiness([{ ...indexRef, requires_index: true }]), [true]);
  console.log('PASS batched readiness: auth/isolation, 512 bound, bytes/kind/index metadata, missing/pending/expired claims, retained and legacy indexes');
  const replay = await rpc('ct_collector_publish_artifacts', publication, 'publication:0');
  delete replay.__benchmark; delete published.__benchmark;
  assert.deepEqual(replay, published);
  const previous = await rpc('ct_artifact_manifest', { project_id: publication.project_id });
  const oversized = { ...publication, publication_sequence: 1, schema_version: 'ct.artifact-manifest.v3', graphs: publication.graphs.map(compactGraph) };
  oversized.graphs[0].vendors = ['雪'.repeat(700000)];
  await rejectPublication(oversized, 'artifact_manifest_too_large', 413);
  const unchanged = await rpc('ct_artifact_manifest', { project_id: publication.project_id });
  assert.deepEqual(unchanged.manifests, previous.manifests);
  assert.equal(unchanged.snapshot_sequence, previous.snapshot_sequence);
  await claimProbe(ref, 'legacy-manifests');
  assert.equal((await rpc('ct_artifact_manifest', { project_id: publication.project_id })).manifests[0].schema_version, 'ct.artifact-manifest.v2');
  const bucket = await mf.getR2Bucket('ARTIFACTS');
  let sequence = 0;
  const publish = () => rpc('ct_collector_publish_artifacts', { ...publication, publication_sequence: ++sequence });
  await claimProbe(ref, 'noise');
  await upload('summary', stable(fixture.summary));
  const bounded = await publish();
  assert.equal((await rpc('ct_artifact_manifest', { project_id: publication.project_id })).manifests[0].schema_version, 'ct.artifact-manifest.v3');
  console.log('PASS stored UTF-8 row guard with atomic rejection, legacy retention and v3 republication');
  const lookups = bounded.__benchmark.groups['publication claim lookups'];
  // Only summary matches; allow index-boundary reads on empty kind batches,
  // but never scan the 1000 unrelated rows per lookup.
  assert.ok(lookups.rowsRead <= 2 * lookups.calls, JSON.stringify(lookups));
  console.log('PASS indexed lookups with 1000 unrelated claims', bounded.__benchmark.groups['publication claim lookups']);
  await claimProbe(ref, 'clear-noise');
  // Successful PUT is stalled before completion; cleanup must preserve its pending fence.
  const body = stable({ ...fixture.facts, qualification: 'pending-through-cleanup' });
  const pendingRef = { kind: 'facts', sha256: sha(body), bytes: Buffer.byteLength(body) };
  await local('object-gate', { operation: 'put', key: objectKey(pendingRef), fail: false });
  const pendingUpload = upload('facts', body);
  await waitGate('object-gate');
  assert.equal((await claimProbe(pendingRef))[0].completion, null);
  const incomplete = structuredClone(publication); incomplete.publication_sequence = sequence + 1; incomplete.graphs[0].facts = pendingRef;
  await rejectPublication(incomplete, 'artifact_upload_incomplete');
  await publish();
  assert.ok(await bucket.head(objectKey(pendingRef)));
  await mf.dispatchFetch('http://local/__benchmark/object-gate', { method: 'DELETE' });
  await pendingUpload;
  assert.deepEqual(await readiness([pendingRef]), [true]);
  await publish();
  assert.ok(await bucket.head(objectKey(pendingRef))); // Completed uncommitted claim also protected.
  await local('claim-expiry', { ...pendingRef, expiresAt: 0 });
  await publish();
  assert.equal(await bucket.head(objectKey(pendingRef)), null);
  assert.deepEqual(await readiness([pendingRef]), [false]);
  await rejectPublication(incomplete, 'artifact_upload_incomplete'); // A formerly ready hint is no lease.
  // A duplicate claim cannot downgrade completion; late completion cannot resurrect release.
  await upload('summary', stable(fixture.summary));
  await local('object-gate', { operation: 'get', key: objectKey(ref), fail: false });
  const duplicate = mf.dispatchFetch(`http://local/v1/artifacts/summary/${ref.sha256}`, {
    method: 'PUT', headers: { authorization: `Bearer ${tokens.owner}` }, body: stable(fixture.summary) });
  await waitGate('object-gate');
  assert.ok((await claimProbe(ref))[0].completion);
  await publish();
  assert.deepEqual(await claimProbe(ref), []);
  await mf.dispatchFetch('http://local/__benchmark/object-gate', { method: 'DELETE' });
  assert.equal((await duplicate).status, 409);
  assert.deepEqual(await claimProbe(ref), []);
  await upload('summary', stable(fixture.summary)); // Retry re-verifies, no PUT.
  // An upload starting during cleanup cannot write until its fence is established.
  await mf.dispatchFetch('http://local/__benchmark/list-gate', { method: 'POST' });
  const publishing = publish();
  await waitGate('list-gate');
  let settled = false;
  const racing = upload('facts', body).then(() => { settled = true; });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(settled, false);
  await mf.dispatchFetch('http://local/__benchmark/list-gate', { method: 'DELETE' });
  await publishing; await racing;
  assert.ok(await bucket.head(objectKey(pendingRef)));
  await rejectPublication({ ...publication, publication_sequence: sequence + 2 }, 'publication_sequence_gap');
  assert.equal((await rpc('ct_collector_publish_artifacts', { ...publication, publication_sequence: sequence })).outcome, 'conflict');
  // Source writes and a second publication must remain behind an in-flight publication.
  await local('object-gate', { operation: 'get', key: objectKey(ref), fail: false });
  const first = publish();
  await waitGate('object-gate');
  const arrivals = await invocationCounts();
  const next = publish();
  await waitInvocation('ct_collector_publish_artifacts', arrivals.ct_collector_publish_artifacts + 1);
  const vector = publication.source_vector[0];
  const checkpointPayload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [2] }, session_digest: fixture.source };
  const checkpointHash = sha(stable(checkpointPayload));
  let checkpointSettled = false;
  const checkpoint = rpc('ct_collector_publish_observation', { agent_id: agent, source_id: vector.source_id,
    source_epoch: vector.source_epoch, source_sequence: 1, event_id: `checkpoint:${checkpointHash}`,
    parser_version: 'qualification.v1', content_sha256: checkpointHash, observed_at: '2026-09-19T00:00:01Z',
    payload: checkpointPayload }).then(result => { checkpointSettled = true; return result; });
  await waitInvocation('ct_collector_publish_observation', arrivals.ct_collector_publish_observation + 1);
  assert.equal(checkpointSettled, false);
  await mf.dispatchFetch('http://local/__benchmark/object-gate', { method: 'DELETE' });
  const firstReceipt = await first, nextReceipt = await next, checkpointReceipt = await checkpoint;
  assert.ok(firstReceipt.committed_sequence < nextReceipt.committed_sequence);
  assert.ok(nextReceipt.committed_sequence < checkpointReceipt.committed_sequence);
  await rejectPublication({ ...publication, publication_sequence: sequence + 1 }, 'source_vector_requires_accepted_project_checkpoints', 400);
  console.log('PASS receipt release/replay, retained index reuse, pending/completed cleanup protection and expiry, duplicate upload/commit race, cleanup/upload race, sequence fences');
}
async function main() { try {
  const project = await rpc('ct_project_register', { agent_id: agent, display_name: 'DirectApi' });
  const source = await rpc('ct_collector_register_source', { agent_id: agent, project_id: project.project_id, vendor: 'pi', native_session_id: fixture.root });
  delete source.__benchmark;
  const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] }, session_digest: fixture.source };
  const content = sha(stable(payload));
  await rpc('ct_collector_publish_observation', { agent_id: agent, ...source, source_sequence: 0, event_id: `checkpoint:${content}`,
    parser_version: 'qualification.v1', content_sha256: content, observed_at: '2026-09-19T00:00:00Z', payload });
  if (qualifyPublication) {
    const legacy = await claimProbe({ kind: 'facts', sha256: sha(stable(fixture.facts)) }, 'legacy');
    assert.equal(legacy[0].completion, null); assert.equal(legacy[0].token, null);
  }
  const facts = await upload('facts', stable(fixture.facts)), summary = await upload('summary', stable(fixture.summary));
  const refs = [];
  for (const [hash, body] of Object.entries(fixture.api.objects)) {
    assert.equal(sha(body), hash); refs.push(await upload('api', body));
  }
  const publication = { agent_id: agent, project_id: project.project_id, publication_sequence: 0,
    source_vector: [{ ...source, source_sequence: 0, content_sha256: content }], graphs: [{
      graph_id: fixture.root, graph_input_sha256: fixture.source, fact_set_digest: fixture.source, fact_count: fixture.facts.rows.length,
      source_ids: [source.source_id], vendors: ['pi'], observed_at: '2026-09-19T00:00:00Z', facts, summary,
      api_methods: fixture.api.methods, api_objects: refs,
    }] };
  const compact = compactGraph(publication.graphs[0]);
  assert.deepEqual(expandGraph(compact), publication.graphs[0]);
  if (fixture.compact_api) assert.deepEqual(compact.api, fixture.compact_api, 'Python/TypeScript producer parity');
  if (fixture.compact_corner) {
    const { expanded, compact: corner } = fixture.compact_corner;
    assert.deepEqual(compactGraph(expanded), corner);
    assert.deepEqual(expandGraph(corner), expanded);
    assert.deepEqual(corner.api.entries, [[0, 0, '雪', 0], [0, 1, null, null], [0, 0, null, 1]]);
  }
  const compactRequest = { ...publication, schema_version: 'ct.artifact-manifest.v3', graphs: [compact] };
  for (const [column, position, code] of [[0, compact.api.methods.length, 'invalid_prepared_reference'],
    [1, compact.api.scopes.length, 'invalid_prepared_reference'], [3, compact.api.objects.length, 'invalid_prepared_reference'],
    [3, -1, 'invalid_contract'], [3, 0.5, 'invalid_contract'], [3, '0', 'invalid_contract']]) {
    const malformed = structuredClone(compactRequest); malformed.graphs[0].api.entries[0][column] = position;
    await rejectPublication(malformed, code, 400);
  }
  const duplicate = structuredClone(compactRequest); duplicate.graphs[0].api.objects.push(duplicate.graphs[0].api.objects[0]);
  await rejectPublication(duplicate, 'invalid_prepared_reference', 400);
  console.log('PASS compact tables: exact producer parity, ordered expansion, invalid positions and duplicate objects');
  const params = { session_id: fixture.root, limit: 200 };
  assert.equal((await api('session.overview', params, 'owner', 409)).error.code, 'prepared_view_unavailable');
  if (qualifyPublication) await qualifyReceiptsBefore(publication);
  if (publicationBenchmark) await mf.dispatchFetch(`http://local/__benchmark/r2?reset&delay=${delayMs}`);
  const publicationStarted = performance.now();
  if (inputGate) {
    assert.ok(delayMs >= 4500, 'counterfactual must exceed 30s before commit');
    const rejected = await post('/v1/core', { protocol: 'ct.core.v1', method: 'ct_collector_publish_artifacts',
      params: { workspace_id: workspace, ...publication } }, 'owner', 503);
    assert.equal(rejected.error.code, 'authority_unavailable');
    const elapsed = performance.now() - publicationStarted;
    await mf.dispatchFetch('http://local/__benchmark/r2?delay=0');
    const manifest = await post('/v1/core', { protocol: 'ct.core.v1', method: 'ct_artifact_manifest',
      params: { workspace_id: workspace, snapshot_sequence: null } }, 'owner', 404);
    assert.equal(manifest.error.code, 'artifact_snapshot_unavailable');
    const report = { localOnly: true, counterfactualInputGate: true, delayMs, wallMs: elapsed, committed: false };
    writeFileSync(process.argv[4], JSON.stringify(report, null, 2) + '\n');
    console.log('PASS expected input-gate timeout with no commit', report);
    return;
  }
  const published = await rpc('ct_collector_publish_artifacts', publication, 'publication:0');
  const publicationWallMs = performance.now() - publicationStarted;
  const manifestReply = await rpc('ct_artifact_manifest', { project_id: project.project_id });
  const storedManifest = manifestReply.manifests[0];
  assert.equal(storedManifest.schema_version, 'ct.artifact-manifest.v3');
  assert.deepEqual(storedManifest.graphs[0].api, compact.api);
  assert.ok(Buffer.byteLength(stable(storedManifest)) < 2 * 1024 * 1024 - 4096);
  if (publicationBenchmark) {
    const report = { localOnly: true, fixtureSha256: sha(readFileSync(process.argv[2])),
      objects: refs.length + 2, indexedMethods: fixture.api.methods.filter(method => method.index).length,
      delayMs, wallMs: publicationWallMs, sql: published.__benchmark,
      r2: await (await mf.dispatchFetch('http://local/__benchmark/r2')).json() };
    await mf.dispatchFetch('http://local/__benchmark/r2?delay=0');
    writeFileSync(process.argv[4], JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
    assert.deepEqual(report.r2.calls, { get: baseline ? report.indexedMethods + 1 : 1,
      ...(baseline ? { head: report.objects } : {}), put: 6, list: Math.ceil((refs.length + 8) / 1000) });
    if (qualifyPublication) await qualifyReceiptsAfter(publication, published);
  }
  if (benchmarking) {
    const { benchmark } = await import('./benchmark-prepared-api.mjs');
    await benchmark({ mf, fixture, bundle: bundle.outputFiles[0].text,
      output: process.argv[4], request: () => api('session.overview', params) });
  } else {
  const first = await api('session.overview', params);
  assert.equal(first.data.project.project_id, project.project_id);
  assert.equal(first.data.sessions[0].cwd, fixture.cwd);
  assert.equal(first.data.turns.at(-1).global_ordinal, lastOrdinal);
  const hash = first.meta.identity.view_manifest_sha256;
  const selected = first.data.turns.at(-1);
  const detail = await api('session.items', { session_id: fixture.root, turn_id: selected.turn_id,
    item_ids: [selected.activities[3].item_id, 'not-present'], view_manifest_sha256: hash, limit: 2 });
  assert.equal(detail.data.items.length, 1); assert.equal(detail.data.items[0].detail.exit_code, 7);
  assert.ok(detail.data.items[0].detail.target.includes(`--ordinal ${lastOrdinal}`)); assert.deepEqual(detail.data.unresolved_ids, ['not-present']);
  const outsideTurn = first.data.turns[0].activities[0].item_id;
  const outside = await api('session.items', { session_id: fixture.root, turn_id: selected.turn_id,
    item_ids: [outsideTurn, 'not-present'], view_manifest_sha256: hash });
  assert.deepEqual(outside.data.items, []); assert.deepEqual(outside.data.unresolved_ids, ['not-present']);
  const events = await api('session.events', { session_id: fixture.root, event_ids: detail.data.items[0].event_ids, view_manifest_sha256: hash });
  assert.equal(events.data.events.length, 1);
  assert.equal(events.data.events[0].item_id, detail.data.items[0].item_id);
  assert.equal(events.data.events[0].type, 'tool.call.failed');
  for (const expected of fixture.column_queries ?? []) {
    let cursor = null;
    const collected = [];
    do {
      const page = (await api('session.events', { ...expected.params, limit: 41, cursor, view_manifest_sha256: hash })).data;
      assert.equal(page.total, expected.events.length);
      collected.push(...page.events);
      cursor = page.next_cursor;
    } while (cursor);
    assert.deepEqual(collected, expected.events);
  }
  if (fixture.column_queries) console.log('PASS large column index: complete pages, filtered totals/order, sparse IDs and combined filters');
  let page = first.data, seen = page.turns.map(row => row.global_ordinal);
  assert.equal(Boolean(page.page.next_cursor), fixture.benchmark?.shape !== 'representative');
  const cursor = page.page.next_cursor;
  while (page.page.next_cursor) {
    page = (await api('session.overview', { ...params, cursor: page.page.next_cursor })).data;
    assert.deepEqual(page.sessions, first.data.sessions);
    assert.deepEqual(page.turns.map(row => row.global_ordinal), page.turns.map(row => row.global_ordinal).sort((a,b) => a-b));
    seen.push(...page.turns.map(row => row.global_ordinal));
  }
  assert.deepEqual(seen.sort((a,b) => a-b), Array.from({ length: lastOrdinal + 1 }, (_,i) => i));
  if (cursor) {
    await api('session.overview', { ...params, cursor: cursor + 'x' }, 'owner', 400);
    await api('session.overview', { ...params, cursor, limit: 1 }, 'owner', 400);
    const expired = JSON.parse(Buffer.from(cursor.split('.')[0], 'base64url'));
    expired.expires = 1;
    const expiredBody = Buffer.from(JSON.stringify(expired)).toString('base64url');
    const expiredCursor = expiredBody + '.' + createHmac('sha256', 'direct-api-local-cursor-key-00000000001').update(expiredBody).digest('base64url');
    await api('session.overview', { ...params, cursor: expiredCursor }, 'owner', 409);
  }
  await api('session.overview', { ...params, view_manifest_sha256: 'f'.repeat(64) }, 'owner', 409);
  await api('session.overview', params, 'collect', 403);
  await api('session.overview', params, 'missing', 401);
  await api('session.overview', { ...params, view_manifest_sha256: hash }, 'other', 409);
  await api('session.overview', params, 'owner', 400, 3);
  await api('session.search', { session_id: fixture.root, query: 'x' }, 'owner', 501, 2);
  const inventory = await api('project.sessions', { limit: 1 });
  assert.equal(inventory.data.items[0].view_manifest_sha256, hash);
  const instance = '00000000-0000-0000-0000-000000000003';
  await rpc('ct_collector_heartbeat', { agent_id: agent, agent_instance_id: instance, observation_sequence: 1, observed_at: new Date().toISOString() });
  for (let n = 1; n <= 3; n++) await rpc('ct_collector_publish_living_observation', {
    agent_id: agent, agent_instance_id: instance, observation_sequence: n + 1, observed_at: new Date().toISOString(), kind: 'living.sessions',
    payload: { cursor: 'producer-local', revision: 0, operation: 'upsert', resource_kind: 'session',
      path: { root_session_id: fixture.root, session_id: `00000000-0000-0000-0000-00000000001${n}` }, resource: null },
  });
  const living = (await api('living.sessions', { limit: 1 }, 'owner', 200, 3)).data;
  assert.equal(living.changes.length, 1); assert.ok(living.has_more);
  const continuation = (await api('living.sessions', { limit: 1, through: living.through, after: living.next_cursor }, 'owner', 200, 3)).data;
  assert.notEqual(living.changes[0].revision, continuation.changes[0].revision);
  assert.equal(living.coverage.evaluated_at, continuation.coverage.evaluated_at);
  await api('living.sessions', { through: living.through + 'x' }, 'owner', 400, 3);
  await api('living.sessions', { through: living.through }, 'other', 400, 3);
  for (const expected of fixture.usage_expected ?? []) for (const limit of [37, 1000]) {
    const fields = expected.method === 'session.request_usage' ? ['requests'] : ['tool_items', 'item_real_token_costs'];
    const collected = Object.fromEntries(fields.filter(field => expected.data[field] != null).map(field => [field, []]));
    let cursor = null;
    do {
      const result = (await api(expected.method, { ...expected.params, limit, cursor, view_manifest_sha256: hash })).data;
      for (const field of Object.keys(collected)) collected[field].push(...result[field]);
      const omitted = new Set([...fields, 'total', 'returned', 'next_cursor', 'unresolved_ids']);
      assert.deepEqual(Object.fromEntries(Object.entries(result).filter(([key]) => !omitted.has(key))),
        Object.fromEntries(Object.entries(expected.data).filter(([key]) => !fields.includes(key))));
      cursor = result.next_cursor;
      if (cursor) await api(expected.method, { ...expected.params, limit: limit - 1, cursor, view_manifest_sha256: hash }, 'owner', 400);
    } while (cursor);
    for (const field of Object.keys(collected)) assert.deepEqual(collected[field], expected.data[field]);
  }
  console.log('PASS paged usage: every detail row and aggregate matches canonical handlers, session/turn scope and cursor binding');
  await api('session.tool_usage', { session_id: fixture.root, view_manifest_sha256: hash });
  await api('session.tool_usage', { session_id: fixture.root, turn_id: selected.turn_id, view_manifest_sha256: hash });
  for (const method of ['session.summary', 'session.tree', 'session.stats', 'session.usage', 'session.model_usage', 'session.request_usage', 'graph.stats', 'graph.usage', 'graph.overview']) {
    const scoped = method.startsWith('graph.') ? { root_session_id: fixture.root } : { session_id: fixture.root };
    await api(method, { ...scoped, view_manifest_sha256: hash });
  }
  const probe = `
import sys, threading, httpx
from uuid import UUID
from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory, build_http_server
url, workspace, token, session, last_ordinal = sys.argv[1:]
factory = RemoteRuntimeFactory(url=url, workspace_id=UUID(workspace))
with factory.build(token) as runtime:
    overview = runtime.call('session.overview', {'session_id': session, 'limit': 1})
    view = runtime.transport_metadata()['identity']['view_manifest_sha256']
    turn = overview['turns'][0]
    detail = runtime.call('session.items', {'session_id': session, 'item_ids': [turn['activities'][3]['item_id']], 'view_manifest_sha256': view})
    assert detail['items'][0]['detail']['exit_code'] == 7
    assert runtime.call('project.list', {})['returned'] == 1
server = build_http_server(factory=factory, port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    response = httpx.post(f'http://127.0.0.1:{server.server_port}/v1/api', headers={'Authorization': f'Bearer {token}'}, json={'protocol': 'ct.api.v1', 'id': 'proxy', 'method': 'session.overview', 'method_version': 4, 'params': {'session_id': session, 'limit': 1}})
    assert response.status_code == 200, response.text
    assert response.json()['data']['turns'][0]['global_ordinal'] == int(last_ordinal)
    assert response.headers['cache-control'] == 'no-store'
finally:
    server.shutdown()
    server.server_close()
    thread.join()
print('PASS owned Python remote runtime and authenticated direct API proxy')
`;
  const address = await mf.ready;
  await new Promise((resolve, reject) => {
    const child = spawn(`${root}.venv/bin/python`, ['-c', probe, address.origin, workspace, tokens.owner, fixture.root, String(lastOrdinal)], { stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve() : reject(new Error(`Python client probe exited ${code}`)));
  });
  const bucket = await mf.getR2Bucket('ARTIFACTS');
  const descriptor = fixture.api.methods.find(value => value.method === 'session.overview');
  const key = `workspaces/${workspace}/artifacts/api/${descriptor.index.sha256}`;
  await bucket.delete(key);
  assert.equal((await api('session.overview', params, 'owner', 503)).error.code, 'prepared_object_missing');
  await bucket.put(key, fixture.api.objects[descriptor.index.sha256].replace('session.overview', 'session.overvieX'));
  assert.equal((await api('session.overview', params, 'owner', 503)).error.code, 'prepared_object_corrupt');
  await bucket.put(key, fixture.api.objects[descriptor.index.sha256] + ' ');
  assert.equal((await api('session.overview', params, 'owner', 503)).error.code, 'prepared_object_corrupt');
  await bucket.put(key, fixture.api.objects[descriptor.index.sha256].slice(0, -1));
  assert.equal((await api('session.overview', params, 'owner', 503)).error.code, 'prepared_object_corrupt');
  console.log('PASS Worker publication readiness, overview -> older pages -> pinned detail, semantic arguments/outcomes, exact methods, inventory, UTF-8 response bounds, auth/isolation, versions, cursor conflicts and corrupt objects');
  }
} finally { await mf.dispose(); } }
await main();
