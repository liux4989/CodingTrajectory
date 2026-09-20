import { Json, requireThat, stable } from "./shared";

/** v3 changes only manifest representation, never prepared object bytes or order. */
export function compactGraph(graph: Json): Json {
  const { api_objects: objects, api_methods: descriptors, ...metadata } = graph;
  const positions = new Map<string, number>(objects.map((ref: Json, i: number) => [ref.sha256, i]));
  const methods: unknown[][] = [], scopes: string[] = [], entries: unknown[][] = [];
  const methodPositions = new Map<string, number>(), scopePositions = new Map<string, number>();
  for (const descriptor of descriptors) {
    const method = [descriptor.method, descriptor.method_version], key = stable(method);
    if (!methodPositions.has(key)) { methodPositions.set(key, methods.length); methods.push(method); }
    if (!scopePositions.has(descriptor.scope)) { scopePositions.set(descriptor.scope, scopes.length); scopes.push(descriptor.scope); }
    const index = descriptor.index ? positions.get(descriptor.index.sha256) : null;
    requireThat(index !== undefined && (!descriptor.index || objects[index!].bytes === descriptor.index.bytes), "invalid_prepared_reference");
    entries.push([methodPositions.get(key), scopePositions.get(descriptor.scope), descriptor.turn_id ?? null, index]);
  }
  return { ...metadata, api: { objects: objects.map((ref: Json) => [ref.sha256, ref.bytes]), methods, scopes, entries } };
}

export function expandGraph(graph: Json): Json {
  const { api, ...metadata } = graph;
  const objects = api.objects.map(([sha256, bytes]: [string, number]) => ({ kind: "api", sha256, bytes }));
  requireThat(new Set(objects.map((ref: Json) => ref.sha256)).size === objects.length, "invalid_prepared_reference");
  const at = (values: any[], position: number) => {
    requireThat(Number.isSafeInteger(position) && position >= 0 && position < values.length, "invalid_prepared_reference");
    return values[position];
  };
  const methods = api.entries.map(([method, scope, turn_id, index]: [number, number, string | null, number | null]) => {
    const [name, version] = at(api.methods, method);
    return { method: name, method_version: version, scope: at(api.scopes, scope), turn_id,
      index: index === null ? null : at(objects, index), error: index === null ? "remote_result_too_large" : null };
  });
  return { ...metadata, api_objects: objects, api_methods: methods };
}

export function expandManifest(manifest: Json): Json {
  return manifest.schema_version === "ct.artifact-manifest.v3"
    ? { ...manifest, schema_version: "ct.artifact-manifest.v2", graphs: manifest.graphs.map(expandGraph) }
    : manifest;
}
