// Disposable Miniflare integration. Never loads Wrangler credentials or remote bindings.
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value !== null && typeof value === 'object'
  ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}` : JSON.stringify(value);
const workspace = '00000000-0000-0000-0000-000000000001', agent = '00000000-0000-0000-0000-000000000002';
const tokens = { owner: 'direct-api-local-owner-token-00000001', collect: 'direct-api-local-collect-token-000001', other: 'direct-api-local-other-token-00000001' };
const principals = Object.fromEntries(Object.entries(tokens).map(([role, token]) => [sha(token), {
  workspace_id: role === 'other' ? '00000000-0000-0000-0000-000000000009' : workspace, agent_id: agent,
  roles: role === 'collect' ? ['collect'] : ['owner'],
}]));
const bundle = await build({ entryPoints: [`${root}cloudflare/control-plane/src/index.ts`], bundle: true, write: false,
  format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const benchmarking = process.argv[3] === '--benchmark';
const mf = new Miniflare(convertV4MiniflareOptions({ ...(benchmarking ? { inspectorPort: 0 } : {}), workers: [{ name: 'direct-api', modules: true, script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-09-10', durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } },
  r2Buckets: ['ARTIFACTS'], bindings: { CT_CURSOR_KEY: 'direct-api-local-cursor-key-00000000001', CT_PRINCIPALS: JSON.stringify(principals) },
}] }));
async function post(path, message, role = 'owner', expected = 200) {
  const response = await mf.dispatchFetch(`http://local${path}`, { method: 'POST', headers: { authorization: `Bearer ${tokens[role] ?? 'invalid'}` }, body: JSON.stringify(message) });
  const raw = await response.text();
  assert.equal(response.status, expected, raw.slice(0, 1000));
  if (path === '/v1/api') assert.ok(Buffer.byteLength(raw) <= 448 * 1024);
  return JSON.parse(raw);
}
async function rpc(method, params) {
  return (await post('/v1/core', { protocol: 'ct.core.v1', method, params: { workspace_id: workspace, ...params } })).data;
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
try {
  const project = await rpc('ct_project_register', { agent_id: agent, display_name: 'DirectApi' });
  const source = await rpc('ct_collector_register_source', { agent_id: agent, project_id: project.project_id, vendor: 'pi', native_session_id: fixture.root });
  const payload = { kind: 'ct.source_checkpoint.v1', source_checkpoint: { segments: [1] }, session_digest: fixture.source };
  const content = sha(stable(payload));
  await rpc('ct_collector_publish_observation', { agent_id: agent, ...source, source_sequence: 0, event_id: `checkpoint:${content}`,
    parser_version: 'qualification.v1', content_sha256: content, observed_at: '2026-09-19T00:00:00Z', payload });
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
  const params = { session_id: fixture.root, limit: 200 };
  assert.equal((await api('session.overview', params, 'owner', 409)).error.code, 'prepared_view_unavailable');
  await rpc('ct_collector_publish_artifacts', publication);
  if (benchmarking) {
    const { benchmark } = await import('./benchmark-prepared-api.mjs');
    await benchmark({ mf, fixture, bundle: bundle.outputFiles[0].text,
      output: process.argv[4], request: () => api('session.overview', params) });
  } else {
  const first = await api('session.overview', params);
  assert.equal(first.data.project.project_id, project.project_id);
  assert.equal(first.data.sessions[0].cwd, fixture.cwd);
  assert.equal(first.data.turns.at(-1).global_ordinal, 96);
  const hash = first.meta.identity.view_manifest_sha256;
  const selected = first.data.turns.at(-1);
  const detail = await api('session.items', { session_id: fixture.root, turn_id: selected.turn_id,
    item_ids: [selected.activities[3].item_id, 'not-present'], view_manifest_sha256: hash, limit: 2 });
  assert.equal(detail.data.items.length, 1); assert.equal(detail.data.items[0].detail.exit_code, 7);
  assert.ok(detail.data.items[0].detail.target.includes('--ordinal 96')); assert.deepEqual(detail.data.unresolved_ids, ['not-present']);
  const outsideTurn = first.data.turns[0].activities[0].item_id;
  const outside = await api('session.items', { session_id: fixture.root, turn_id: selected.turn_id,
    item_ids: [outsideTurn, 'not-present'], view_manifest_sha256: hash });
  assert.deepEqual(outside.data.items, []); assert.deepEqual(outside.data.unresolved_ids, ['not-present']);
  const events = await api('session.events', { session_id: fixture.root, event_ids: detail.data.items[0].event_ids, view_manifest_sha256: hash });
  assert.equal(events.data.events.length, 1);
  assert.equal(events.data.events[0].item_id, detail.data.items[0].item_id);
  assert.equal(events.data.events[0].type, 'tool.call.failed');
  let page = first.data, seen = page.turns.map(row => row.global_ordinal);
  assert.ok(page.page.next_cursor);
  const cursor = page.page.next_cursor;
  while (page.page.next_cursor) {
    page = (await api('session.overview', { ...params, cursor: page.page.next_cursor })).data;
    assert.deepEqual(page.sessions, first.data.sessions);
    assert.deepEqual(page.turns.map(row => row.global_ordinal), page.turns.map(row => row.global_ordinal).sort((a,b) => a-b));
    seen.push(...page.turns.map(row => row.global_ordinal));
  }
  assert.deepEqual(seen.sort((a,b) => a-b), Array.from({ length: 97 }, (_,i) => i));
  await api('session.overview', { ...params, cursor: cursor + 'x' }, 'owner', 400);
  await api('session.overview', { ...params, cursor, limit: 1 }, 'owner', 400);
  const expired = JSON.parse(Buffer.from(cursor.split('.')[0], 'base64url'));
  expired.expires = 1;
  const expiredBody = Buffer.from(JSON.stringify(expired)).toString('base64url');
  const expiredCursor = expiredBody + '.' + createHmac('sha256', 'direct-api-local-cursor-key-00000000001').update(expiredBody).digest('base64url');
  await api('session.overview', { ...params, cursor: expiredCursor }, 'owner', 409);
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
  // The exact tool ledger for 776 long-command items exceeds the unpaged
  // result bound. Its independently scoped one-turn ledger remains available.
  await api('session.tool_usage', { session_id: fixture.root, view_manifest_sha256: hash }, 'owner', 413);
  await api('session.tool_usage', { session_id: fixture.root, turn_id: selected.turn_id, view_manifest_sha256: hash });
  for (const method of ['session.summary', 'session.tree', 'session.stats', 'session.usage', 'session.model_usage', 'session.request_usage', 'graph.stats', 'graph.usage', 'graph.overview']) {
    const scoped = method.startsWith('graph.') ? { root_session_id: fixture.root } : { session_id: fixture.root };
    await api(method, { ...scoped, view_manifest_sha256: hash });
  }
  const probe = `
import sys, threading, httpx
from uuid import UUID
from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory, build_http_server
url, workspace, token, session = sys.argv[1:]
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
    assert response.json()['data']['turns'][0]['global_ordinal'] == 96
    assert response.headers['cache-control'] == 'no-store'
finally:
    server.shutdown()
    server.server_close()
    thread.join()
print('PASS owned Python remote runtime and authenticated direct API proxy')
`;
  const address = await mf.ready;
  await new Promise((resolve, reject) => {
    const child = spawn(`${root}.venv/bin/python`, ['-c', probe, address.origin, workspace, tokens.owner, fixture.root], { stdio: 'inherit' });
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
  console.log('PASS Worker publication readiness, overview -> older pages -> pinned detail, semantic arguments/outcomes, exact methods, inventory, UTF-8 response bounds, auth/isolation, versions, cursor conflicts and corrupt objects');
  }
} finally { await mf.dispose(); }
