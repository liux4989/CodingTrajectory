/** Start the real Python Worker in disposable local workerd via Wrangler config. */
import { createRequire } from 'node:module';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { extname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const defaultConfigPath = `${root}cloudflare/control-plane/wrangler.jsonc`;
const require = createRequire(`${root}cloudflare/control-plane/package.json`);
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');
const { unstable_getMiniflareWorkerOptions, unstable_readConfig } = require('wrangler');

function rejectRemoteBindings(value, path = 'workerOptions') {
  if (value === null || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    const childPath = `${path}.${key}`;
    if (key === 'remoteProxyConnectionString' && child !== undefined) {
      throw new Error(`local Python Worker harness refuses remote binding ${childPath}`);
    }
    rejectRemoteBindings(child, childPath);
  }
}

function files(directory) {
  if (!existsSync(directory)) return [];
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

function pythonModules(main, entrypoint) {
  const sourceRoot = resolve(main, '..');
  const projectRoot = resolve(sourceRoot, '..');
  const entry = { type: 'PythonModule', path: entrypoint ? 'qualification.py' : 'index.py',
    contents: readFileSync(entrypoint ?? main, 'utf8') };
  const source = files(sourceRoot)
    .filter(path => (entrypoint || path !== main) && ['.py', '.json', '.txt'].includes(extname(path)))
    .map(path => ({
      type: extname(path) === '.py' ? 'PythonModule' : 'Data',
      path: relative(sourceRoot, path).split(sep).join('/'),
      contents: extname(path) === '.py' ? readFileSync(path, 'utf8') : readFileSync(path),
    }));
  const vendorRoot = resolve(projectRoot, 'python_modules');
  const vendor = files(vendorRoot).map(path => {
    const name = `python_modules/${relative(vendorRoot, path).split(sep).join('/')}`;
    const workersJavaScript = name.startsWith('python_modules/workers/')
      && ['.js', '.mjs', '.cjs'].includes(extname(path));
    return { type: workersJavaScript ? 'ESModule' : 'Data', path: name,
      contents: workersJavaScript ? readFileSync(path, 'utf8') : readFileSync(path) };
  });
  return [entry, ...source, ...vendor];
}

/**
 * This deliberately uses Wrangler's config adapter rather than copying runtime
 * flags and binding declarations into each integration harness. Run
 * `uv run --project cloudflare/control-plane pywrangler sync` first so imported
 * packages are present in cloudflare/control-plane/python_modules.
 */
export function pythonWorkerRuntimeOptions({
  name = 'coding-trajectory-python-local',
  configPath = defaultConfigPath,
  entrypoint,
  bindings = {},
  inspectorPort,
  log,
  verbose,
  durableObjectsPersist,
  r2Persist,
  unsafeHandleUncaughtError,
} = {}) {
  const loadedConfig = unstable_readConfig({ config: configPath }, { hideWarnings: true });
  // Never load .dev.vars, .env, process secrets, or config vars in a disposable
  // harness. Every required synthetic value must be supplied by the caller.
  const localConfig = { ...loadedConfig, secrets: undefined, vars: {} };
  const { workerOptions, main, externalWorkers } = unstable_getMiniflareWorkerOptions(
    localConfig, undefined, { envFiles: ['.codex-harness-no-env'] });
  rejectRemoteBindings(workerOptions);
  rejectRemoteBindings(externalWorkers, 'externalWorkers');
  if (externalWorkers.length) throw new Error('local Python Worker harness refuses external workers');
  if (!main?.endsWith('.py')) {
    throw new Error(`expected Python Worker entrypoint, received ${main ?? 'none'}`);
  }
  const worker = {
    ...workerOptions,
    name,
    rootPath: resolve(main, '..'),
    modulesRoot: resolve(main, '..'),
    modules: pythonModules(main, entrypoint),
    bindings: { ...workerOptions.bindings, ...bindings },
    ...(durableObjectsPersist ? { durableObjectsPersist } : {}),
    ...(r2Persist ? { r2Persist } : {}),
  };
  delete worker.modulesRules;
  return convertV4MiniflareOptions({
    ...(inspectorPort === undefined ? {} : { inspectorPort }),
    ...(log === undefined ? {} : { log }),
    ...(verbose === undefined ? {} : { verbose }),
    ...(unsafeHandleUncaughtError ? { unsafeHandleUncaughtError } : {}),
    workers: [worker, ...externalWorkers],
  });
}

export function createPythonWorkerRuntime(options = {}) {
  return new Miniflare(pythonWorkerRuntimeOptions(options));
}

export const pythonWorkerSource = `${root}cloudflare/control-plane/src/index.py`;
