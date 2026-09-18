#!/usr/bin/env node
// Disposable workerd/SQLite integration qualification, never a production client.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { build } from '../cloudflare/control-plane/node_modules/esbuild/lib/main.js';
import { Miniflare, convertV4MiniflareOptions } from '../cloudflare/control-plane/node_modules/miniflare/dist/src/index.js';

const root = resolve(import.meta.dirname, '..');
const directory = await mkdtemp(resolve(tmpdir(), 'ct-legacy-cleanup-'));
const tables = ['fact_rows', 'fact_schema', 'staged_fact_rows', 'staged_fact_items',
  'staged_fact_generations', 'validated_fact_graphs'];
const target = '00000000-0000-0000-0000-000000000001';
const other = '00000000-0000-0000-0000-000000000002';
const entry = `
import { Workspace as RealWorkspace } from './cloudflare/control-plane/src/workspace.ts';
import { dropEmptyLegacyFactTables, legacyFactTables } from './cloudflare/control-plane/src/legacy-cleanup.ts';
import { State } from './cloudflare/control-plane/src/shared.ts';
export class Workspace extends RealWorkspace {
  async fetch(request) {
    const input = await request.json();
    const sql = this.ctx.storage.sql;
    const state = new State(sql);
    const tables = ${JSON.stringify(tables)};
    if (input.action === 'seed') {
      for (const table of tables) sql.exec(table === 'fact_schema'
        ? 'CREATE TABLE fact_schema(id INTEGER, version INTEGER)'
        : 'CREATE TABLE '+table+'(value TEXT)');
      sql.exec('INSERT INTO fact_schema VALUES(1,1)');
      sql.exec('UPDATE sequence SET value=36');
      state.put('project','preserve',{display_name:'preserve'},36);
      if (input.nonempty) sql.exec('INSERT INTO '+input.nonempty+" VALUES('preserve')");
      if (input.schema) sql.exec('UPDATE fact_schema SET version=99');
      if (input.publication) state.put('graph_publication','preserve',{project_id:'preserve'},36);
    }
    let error = null;
    if (input.action === 'cleanup') {
      try { this.ctx.storage.transactionSync(() => dropEmptyLegacyFactTables(state)); }
      catch (failure) { error = failure.code; }
    }
    return Response.json({ ...legacyFactTables(state), error, snapshot: state.head(),
      project: state.get('project','preserve'),
      all_tables: sql.exec("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").toArray() });
  }
}
export default { fetch() { return new Response('qualification only'); } };
`;
const bundled = await build({ stdin: { contents: entry, resolveDir: root, loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'neutral', external: ['cloudflare:workers'] });
const options = {
  modules: true, script: bundled.outputFiles[0].text,
  compatibilityDate: '2026-09-10', compatibilityFlags: ['nodejs_compat'],
  durableObjects: { WORKSPACES: { className: 'Workspace', useSQLite: true } },
  durableObjectsPersist: resolve(directory, 'do'),
  r2Buckets: ['ARTIFACTS'], r2Persist: resolve(directory, 'r2'),
};
const mf = new Miniflare(convertV4MiniflareOptions(options));
let checks = 0;
function check(condition) { assert.ok(condition); checks++; }
async function call(name, body) {
  const ns = await mf.getDurableObjectNamespace('WORKSPACES');
  const result = await ns.get(ns.idFromName(name)).fetch('http://qualification/', {
    method: 'POST', body: JSON.stringify(body),
  });
  assert.equal(result.status, 200);
  return result.json();
}
try {
  const fresh = await call('fresh', {});
  check(Object.keys(fresh.tables).length === 0);
  const before = await call(target, { action: 'seed' });
  const otherBefore = await call(other, { action: 'seed' });
  check(Object.keys(before.tables).length === 6 && before.tables.fact_schema === 1);
  for (const table of tables.filter(value => value !== 'fact_schema')) {
    const name = 'blocked-' + table;
    const seeded = await call(name, { action: 'seed', nonempty: table });
    const blocked = await call(name, { action: 'cleanup' });
    check(blocked.error === 'legacy_cleanup_data_present');
    assert.deepEqual(blocked.tables, seeded.tables); checks++;
  }
  await call('schema', { action: 'seed', schema: true });
  const schema = await call('schema', { action: 'cleanup' });
  check(schema.error === 'legacy_cleanup_schema_unexpected' && Object.keys(schema.tables).length === 6);
  await call('publication', { action: 'seed', publication: true });
  const publication = await call('publication', { action: 'cleanup' });
  check(publication.error === 'legacy_cleanup_data_present' && Object.keys(publication.tables).length === 6);
  const bucket = await mf.getR2Bucket('ARTIFACTS');
  await bucket.put('sentinel', 'untouched');
  await mf.setOptions(convertV4MiniflareOptions({ ...options, bindings: { CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID: target } }));
  const after = await call(target, {});
  check(Object.keys(after.tables).length === 0);
  check(after.snapshot === 36);
  assert.deepEqual(after.project, before.project); checks++;
  assert.deepEqual(await call(other, {}), otherBefore); checks++;
  check(await (await (await mf.getR2Bucket('ARTIFACTS')).get('sentinel')).text() === 'untouched');
  const expectedTables = before.all_tables.filter(row => !tables.includes(row.name));
  assert.deepEqual(after.all_tables, expectedTables); checks++;
  await mf.setOptions(convertV4MiniflareOptions(options));
  assert.deepEqual(await call(target, {}), after); checks++;
  const replay = await call(target, { action: 'cleanup' });
  assert.deepEqual(replay, after); checks++;
  console.log(JSON.stringify({ status: 'ok', checks, dropped_tables: tables,
    snapshot: after.snapshot, other_workspace_preserved: true, r2_preserved: true }));
} finally {
  await mf.dispose();
  await rm(directory, { recursive: true, force: true });
}
