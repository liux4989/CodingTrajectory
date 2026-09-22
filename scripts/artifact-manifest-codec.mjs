/** JavaScript copy of the wire-only v3 manifest table codec used by harnesses. */
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]`
  : value !== null && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
    : JSON.stringify(value);

function requireReference(condition) {
  if (!condition) throw new Error('invalid_prepared_reference');
}

export function compactGraph(graph) {
  const { api_objects: objects, api_methods: descriptors, ...metadata } = graph;
  const positions = new Map(objects.map((ref, index) => [ref.sha256, index]));
  const methods = [], scopes = [], entries = [];
  const methodPositions = new Map(), scopePositions = new Map();
  for (const descriptor of descriptors) {
    const method = [descriptor.method, descriptor.method_version], key = stable(method);
    if (!methodPositions.has(key)) { methodPositions.set(key, methods.length); methods.push(method); }
    if (!scopePositions.has(descriptor.scope)) { scopePositions.set(descriptor.scope, scopes.length); scopes.push(descriptor.scope); }
    const index = descriptor.index ? positions.get(descriptor.index.sha256) : null;
    requireReference(index !== undefined && (!descriptor.index || objects[index].bytes === descriptor.index.bytes));
    entries.push([methodPositions.get(key), scopePositions.get(descriptor.scope), descriptor.turn_id ?? null, index]);
  }
  return { ...metadata, api: { objects: objects.map(ref => [ref.sha256, ref.bytes]), methods, scopes, entries } };
}

export function expandGraph(graph) {
  const { api, ...metadata } = graph;
  const objects = api.objects.map(([sha256, bytes]) => ({ kind: 'api', sha256, bytes }));
  requireReference(new Set(objects.map(ref => ref.sha256)).size === objects.length);
  const at = (values, position) => {
    requireReference(Number.isSafeInteger(position) && position >= 0 && position < values.length);
    return values[position];
  };
  const methods = api.entries.map(([method, scope, turn_id, index]) => {
    const [name, version] = at(api.methods, method);
    return { method: name, method_version: version, scope: at(api.scopes, scope), turn_id,
      index: index === null ? null : at(objects, index), error: index === null ? 'remote_result_too_large' : null };
  });
  return { ...metadata, api_objects: objects, api_methods: methods };
}
