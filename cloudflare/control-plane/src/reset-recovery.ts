import type { SubtleCrypto as WorkerSubtleCrypto } from "@cloudflare/workers-types";
import { DIGEST, fields, object, Principal, uuid } from "./shared";

// Temporary recovery for the approved reset only. Remove with the reset code.
const WORKSPACE = "fbeca960-ce47-4126-ad21-cca95e1855ae";
const METHODS = new Set([
  "ct_connection_status", "ct_workspace_reset", "ct_workspace_snapshot", "ct_project_inventory_snapshot",
]);

export function recoveryAllows(method: string): boolean {
  return METHODS.has(method);
}

export function resetRecoveryPrincipal(encoded: string | undefined, tokenDigest: string): Principal | null {
  if (!encoded) return null;
  try {
    const record = object(JSON.parse(encoded));
    const keys = ["workspace_id", "agent_id", "token_sha256", "not_before_ms", "expires_at_ms"];
    fields(record, keys, keys);
    if (record.workspace_id !== WORKSPACE || typeof record.token_sha256 !== "string" ||
        !DIGEST.test(record.token_sha256) || !Number.isSafeInteger(record.not_before_ms) ||
        !Number.isSafeInteger(record.expires_at_ms)) return null;
    const now = Date.now();
    if (record.not_before_ms > now || record.expires_at_ms <= now ||
        record.expires_at_ms <= record.not_before_ms ||
        record.expires_at_ms - record.not_before_ms > 60 * 60 * 1000) return null;
    const encoder = new TextEncoder();
    // The WebWorker lib omits this Workers runtime extension.
    if (!(crypto.subtle as WorkerSubtleCrypto).timingSafeEqual(
      encoder.encode(record.token_sha256), encoder.encode(tokenDigest),
    )) return null;
    return { workspace_id: WORKSPACE, agent_id: uuid(record.agent_id), roles: ["owner"] };
  } catch {
    // An invalid recovery binding cannot authenticate or disrupt existing principals.
    return null;
  }
}
