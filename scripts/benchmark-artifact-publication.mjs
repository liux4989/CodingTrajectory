#!/usr/bin/env node
// Current prepared-artifact publication benchmark in disposable Python workerd.
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = fileURLToPath(new URL('../', import.meta.url));
const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    shape: { type: 'string', default: 'representative' },
    'delay-ms': { type: 'string', default: '0' },
  },
});
const delay = Number(values['delay-ms']);
if (positionals.length !== 1 || !['representative', 'near-budget', 'index-heavy'].includes(values.shape)
    || !Number.isFinite(delay) || delay < 0 || delay > 1000) {
  throw Error('usage: node scripts/benchmark-artifact-publication.mjs OUTPUT [--shape representative|near-budget|index-heavy] [--delay-ms 0..1000]. The retired v1 GRAPHS/ORPHANS corpus is no longer supported.');
}
const output = resolve(positionals[0]);
const temporary = mkdtempSync(join(tmpdir(), 'ct-python-publication-'));
try {
  const fixture = join(temporary, 'fixture.json');
  mkdirSync(dirname(output), { recursive: true });
  execFileSync('uv', ['run', 'python', 'scripts/qualify-prepared-api.py',
    '--shape', values.shape, '--fixture-output', fixture], { cwd: root, stdio: 'inherit' });
  execFileSync(process.execPath, ['scripts/qualify-prepared-api.mjs', fixture,
    '--publication-timing', output, String(delay)], { cwd: root, stdio: 'inherit' });
  const report = JSON.parse(readFileSync(output, 'utf8'));
  Object.assign(report, {
    benchmark: 'ct.python-prepared-publication.v1',
    shape: values.shape,
    units: 'Local workerd publication wall milliseconds, SQL cursor counters, and R2 binding calls; not Cloudflare billing or production latency.',
    comparison: 'Uses current prepared-artifact contracts; not directly comparable to the retired v1 graph-count corpus.',
  });
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
