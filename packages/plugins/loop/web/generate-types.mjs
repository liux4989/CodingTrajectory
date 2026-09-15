// Generate consumer types from the frozen authority, never a copied Loop schema.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { compile } from "json-schema-to-typescript";
const snapshot = JSON.parse(
  await readFile(
    new URL("../../../../validation/core-protocol.json", import.meta.url),
  ),
);
const directory = new URL("./src/generated/", import.meta.url);
await mkdir(directory, { recursive: true });
const schemas = Object.fromEntries(
  [
    "project.list",
    "project.sessions",
    "session.summary",
    "session.overview",
    "session.items",
    "session.events",
    "session.usage",
    "session.request_usage",
  ].map((method) => [method, snapshot.methods[method].result]),
);
const loopModels = {
  investigation: "Investigation",
  monitor: [
    "StrategyManifest",
    "Watch",
    "Evaluation",
    "Finding",
    "DryRunResult",
    "RefreshResult",
  ],
};
schemas.investigation = loopModelSchema(loopModels.investigation);
for (const model of loopModels.monitor) {
  schemas[`monitor.${model.toLowerCase()}`] = loopModelSchema(model);
}
for (const [name, schema] of Object.entries(schemas)) {
  const target = new URL(`${name}.ts`, directory);
  const result = await compile(schema, schema.title, {
    bannerComment:
      "/* Generated from Core freeze / Loop Pydantic. Do not edit. */",
    additionalProperties: false,
  });
  if (process.argv.includes("--check")) {
    if ((await readFile(target, "utf8")) !== result)
      throw new Error(`Stale generated type: ${name}`);
  } else await writeFile(target, result);
}
console.log("Loop consumer types: PASS");

function loopModelSchema(model) {
  return JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "--package",
        "ct-plugin-loop",
        "python",
        "-c",
        `import json, importlib
models = importlib.import_module("loop_plugin.models")
monitor = importlib.import_module("loop_plugin.monitor.models")
strategies = importlib.import_module("loop_plugin.monitor.strategies")
model = getattr(models, "${model}", None) or getattr(monitor, "${model}", None) or getattr(strategies, "${model}")
print(json.dumps(model.model_json_schema()))`,
      ],
      { encoding: "utf8" },
    ),
  );
}
