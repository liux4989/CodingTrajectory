#!/usr/bin/env node
/** Exercise the exact staging driver against disposable local Miniflare. */
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { build } = require('esbuild');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const sha = value => createHash('sha256').update(value).digest('hex');
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
const publication = join(directory, 'publication.json');
const preflight = join(directory, 'preflight.json');
const full = join(directory, 'full.json');
try {
  const address = await mf.ready;
  const env = { ...process.env, CT_STAGING_URL: address.origin, CT_STAGING_ALLOW_HTTP: '1',
    CT_STAGING_PUBLICATION_TOKEN: token, CT_STAGING_WORKSPACE_ID: workspace,
    CT_STAGING_AGENT_ID: agent, CT_STAGING_EXPECTED_VERSION: version };
  for (const args of [
    ['publish', plan, publication],
    ['measure', plan, publication, preflight, '--preflight'],
    ['measure', plan, publication, full, '--full'],
  ]) {
    await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [`${root}scripts/run-prepared-api-staging.mjs`, ...args],
        { cwd: root, env, stdio: 'inherit' });
      child.on('error', reject);
      child.on('exit', code => code === 0 ? resolve() : reject(Error(`driver exited ${code}`)));
    });
  }
  const publicationResult = JSON.parse(readFileSync(publication));
  const preflightResult = JSON.parse(readFileSync(preflight));
  const fullResult = JSON.parse(readFileSync(full));
  if (publicationResult.status !== 'PASS' || preflightResult.status !== 'PASS'
      || fullResult.status !== 'PASS' || preflightResult.http_requests !== 8
      || fullResult.http_requests !== 602) throw Error('staging driver qualification mismatch');
  console.log(`PASS exact staging driver: publication=${publicationResult.http_requests} HTTP/${publicationResult.object_puts} PUT, preflight=8, full=602`);
} finally {
  await mf.dispose();
  rmSync(directory, { recursive: true, force: true });
}
