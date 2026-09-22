#!/usr/bin/env node
// Disposable Python workerd/SQLite integration qualification.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { createPythonWorkerRuntime, pythonWorkerRuntimeOptions } from './python-worker-runtime.mjs';

const root = resolve(import.meta.dirname, '..');
const directory = await mkdtemp(resolve(tmpdir(), 'ct-legacy-cleanup-'));
const entrypoint = resolve(root, 'scripts/legacy-cleanup-worker.py');
const tables = ['fact_rows', 'fact_schema', 'staged_fact_rows', 'staged_fact_items',
  'staged_fact_generations', 'validated_fact_graphs'];
const target = '00000000-0000-0000-0000-000000000001';
const other = '00000000-0000-0000-0000-000000000002';
const persistence = { durableObjectsPersist: resolve(directory, 'do'), r2Persist: resolve(directory, 'r2') };
const options = bindings => ({ name: 'legacy-cleanup-qualification', entrypoint, bindings, ...persistence });
const runtime = bindings => createPythonWorkerRuntime(options(bindings));
let mf = runtime({});
let checks = 0;
function check(condition) { assert.ok(condition); checks++; }
async function call(name, body) {
  const namespace = await mf.getDurableObjectNamespace('WORKSPACES');
  return JSON.parse(JSON.stringify(await namespace.get(namespace.idFromName(name)).probe(body)));
}
try {
  const fresh = await call('fresh', {});
  check(Object.keys(fresh.tables).length === 0);
  const before = await call(target, { action: 'seed' });
  const otherBefore = await call(other, { action: 'seed' });
  check(Object.keys(before.tables).length === 6 && before.tables.fact_schema === 1);
  for (const table of tables.filter(value => value !== 'fact_schema')) {
    const name = `blocked-${table}`;
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

  await mf.setOptions(pythonWorkerRuntimeOptions(options({ CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID: target })));
  const after = await call(target, {});
  check(Object.keys(after.tables).length === 0);
  check(after.snapshot === 36);
  assert.deepEqual(after.project, before.project); checks++;
  assert.deepEqual(await call(other, {}), otherBefore); checks++;
  check(await (await (await mf.getR2Bucket('ARTIFACTS')).get('sentinel')).text() === 'untouched');
  const expectedTables = before.all_tables.filter(row => !tables.includes(row.name));
  assert.deepEqual(after.all_tables, expectedTables); checks++;

  await mf.setOptions(pythonWorkerRuntimeOptions(options({})));
  assert.deepEqual(await call(target, {}), after); checks++;
  const replay = await call(target, { action: 'cleanup' });
  assert.deepEqual(replay, after); checks++;
  console.log(JSON.stringify({ status: 'ok', checks, dropped_tables: tables,
    snapshot: after.snapshot, other_workspace_preserved: true, r2_preserved: true }));
} finally {
  await mf.dispose();
  await rm(directory, { recursive: true, force: true });
}
