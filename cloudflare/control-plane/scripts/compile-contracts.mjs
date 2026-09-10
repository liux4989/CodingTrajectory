import { readFile, writeFile } from "node:fs/promises";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standalone from "ajv/dist/standalone/index.js";

const schemas = JSON.parse(await readFile(new URL("../src/contracts.json", import.meta.url), "utf8"));
const ajv = new Ajv({ strict: false, allErrors: false, useDefaults: true, code: { source: true, esm: true } });
addFormats(ajv);
for (const [name, schema] of Object.entries(schemas)) ajv.addSchema(schema, name);
await writeFile(new URL("../src/validators.js", import.meta.url), standalone(ajv, Object.fromEntries(Object.keys(schemas).map(name => [name, name]))));
