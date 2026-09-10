import * as validators from "./validators.js";

export type Json = Record<string, any>;
export type Principal = { workspace_id: string; agent_id: string; roles: string[] };
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const DIGEST = /^[0-9a-f]{64}$/;
export const MAX_BODY = 16 * 1024 * 1024;
export const MAX_ARTIFACT = 8 * 1024 * 1024;

export class Fault extends Error {
  constructor(public status: number, public code: string) { super(code); }
}
export function requireThat(value: unknown, code: string, status = 400): asserts value {
  if (!value) throw new Fault(status, code);
}
export function object(value: unknown): Json {
  requireThat(value !== null && typeof value === "object" && !Array.isArray(value), "object_required");
  return value as Json;
}
export function fields(value: Json, allowed: string[], required: string[] = []) {
  requireThat(Object.keys(value).every(key => allowed.includes(key)) && required.every(key => key in value), "invalid_fields");
}
export function integer(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  requireThat(Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max, "invalid_integer");
  return Number(value);
}
export function text(value: unknown, max = 512): string {
  requireThat(typeof value === "string" && value.length > 0 && value.length <= max, "invalid_string");
  return value as string;
}
export function uuid(value: unknown): string {
  const result = text(value, 36);
  requireThat(UUID.test(result), "invalid_uuid");
  return result.toLowerCase();
}
export function timestamp(value: unknown): string {
  const result = text(value, 64);
  requireThat(Number.isFinite(Date.parse(result)), "invalid_timestamp");
  return result;
}
export function validate(name: string, value: unknown) {
  const validator = (validators as Record<string, (value: unknown) => boolean>)[name];
  if (validator) requireThat(validator(value), "invalid_contract");
}
export function stable(value: any): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
export async function digest(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as BufferSource)), byte => byte.toString(16).padStart(2, "0")).join("");
}
export async function bounded(stream: ReadableStream<Uint8Array> | null, limit = MAX_BODY): Promise<Uint8Array> {
  requireThat(stream, "body_required");
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > limit) { await reader.cancel(); throw new Fault(413, "body_too_large"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}
export function encode(bytes: Uint8Array): string {
  let result = "";
  for (let offset = 0; offset < bytes.length; offset += 16384) result += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
  return btoa(result);
}
export function decode(value: string): Uint8Array {
  try { return Uint8Array.from(atob(value), char => char.charCodeAt(0)); }
  catch { throw new Fault(400, "invalid_base64"); }
}
export function receipt(outcome: string, sequence: number | null, details: Json = {}) {
  return { receipt_id: crypto.randomUUID(), outcome, committed_sequence: sequence, details };
}

export class State {
  constructor(public sql: SqlStorage) {
    sql.exec(`CREATE TABLE IF NOT EXISTS sequence (id INTEGER PRIMARY KEY CHECK (id=1), value INTEGER NOT NULL);
      INSERT OR IGNORE INTO sequence VALUES(1,0);
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, key TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(kind,key,sequence));
      CREATE INDEX IF NOT EXISTS records_sequence ON records(kind,sequence);
      CREATE TABLE IF NOT EXISTS resources (digest TEXT NOT NULL, resource_id TEXT NOT NULL, PRIMARY KEY(digest,resource_id));`);
  }
  head(): number { return this.sql.exec<{ value: number }>("SELECT value FROM sequence WHERE id=1").one().value; }
  next(): number { return this.sql.exec<{ value: number }>("UPDATE sequence SET value=value+1 WHERE id=1 RETURNING value").one().value; }
  pin(value?: unknown): number {
    const head = this.head();
    return value === undefined || value === null ? head : integer(value, 0, head);
  }
  get(kind: string, key: string, sequence = this.head()): Json | undefined {
    const rows = this.sql.exec<{ payload: string }>("SELECT payload FROM records WHERE kind=? AND key=? AND sequence<=? ORDER BY sequence DESC LIMIT 1", kind, key, sequence).toArray();
    return rows.length ? JSON.parse(rows[0].payload) : undefined;
  }
  all(kind: string, sequence = this.head()): Json[] {
    const rows = this.sql.exec<{ payload: string }>(`SELECT r.payload FROM records r JOIN
      (SELECT key,MAX(sequence) AS sequence FROM records WHERE kind=? AND sequence<=? GROUP BY key) latest
      ON r.key=latest.key AND r.sequence=latest.sequence WHERE r.kind=? LIMIT 10001`, kind, sequence, kind).toArray();
    requireThat(rows.length <= 10000, "workspace_query_limit", 413);
    return rows.map(row => JSON.parse(row.payload));
  }
  put(kind: string, key: string, payload: Json, sequence: number) {
    const encoded = JSON.stringify(payload);
    requireThat(new TextEncoder().encode(encoded).length <= 1024 * 1024, "record_too_large", 413);
    this.sql.exec("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,key,sequence) DO UPDATE SET payload=excluded.payload", kind, key, sequence, encoded);
  }
}
