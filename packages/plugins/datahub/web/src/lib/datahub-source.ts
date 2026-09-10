import type { DatahubSnapshot } from "@/api";

export type DatahubSourceKind = "local" | "remote";
export type DatahubCapability =
  | "sessions"
  | "graphs"
  | "session-detail"
  | "today"
  | "compare"
  | "code-time";

export type DatahubSourceProfile = {
  kind: DatahubSourceKind;
  label: string;
  detail: string;
  capabilities: ReadonlySet<DatahubCapability>;
};

const ALL_CAPABILITIES = new Set<DatahubCapability>([
  "sessions",
  "graphs",
  "session-detail",
  "today",
  "compare",
  "code-time",
]);

const REMOTE_SNAPSHOT_CAPABILITIES = new Set<DatahubCapability>([
  "sessions",
  "graphs",
]);

const DEFAULT_LOCAL_ORIGIN = "http://127.0.0.1:8765";
const DEFAULT_REMOTE_ORIGIN =
  "https://coding-trajectory-datahub-preview-candidate.liux4989.workers.dev";

function coverageMode(snapshot: DatahubSnapshot): string | null {
  const coverage = snapshot.bootstrap.coverage;
  if (!coverage || typeof coverage !== "object") return null;
  const mode = coverage.mode;
  return typeof mode === "string" ? mode : null;
}

export function sourceProfile(snapshot: DatahubSnapshot): DatahubSourceProfile {
  const isRemoteSnapshot =
    snapshot.transport?.source === "remote" && coverageMode(snapshot) === "snapshot";
  return isRemoteSnapshot
    ? {
        kind: "remote",
        label: "Remote",
        detail: `Published ${snapshot.horizon_days}-day snapshot`,
        capabilities: REMOTE_SNAPSHOT_CAPABILITIES,
      }
    : {
        kind: "local",
        label: "Local",
        detail: "Live sources and full analysis",
        capabilities: ALL_CAPABILITIES,
      };
}

export function hasCapability(
  profile: DatahubSourceProfile | null,
  capability: DatahubCapability,
): boolean {
  return profile == null || profile.capabilities.has(capability);
}

function configuredOrigin(kind: DatahubSourceKind): string {
  const configured = kind === "local"
    ? import.meta.env.VITE_DATAHUB_LOCAL_URL
    : import.meta.env.VITE_DATAHUB_REMOTE_URL;
  return (configured?.trim() || (kind === "local" ? DEFAULT_LOCAL_ORIGIN : DEFAULT_REMOTE_ORIGIN))
    .replace(/\/$/, "");
}

function safeInternalPath(rawPath: string): string {
  if (!rawPath.startsWith("/") || rawPath.startsWith("//")) return "/sessions";
  return rawPath;
}

function remotePath(rawPath: string): string {
  const path = safeInternalPath(rawPath);
  const detail = path.match(/^\/graphs\/([^/?#]+)\/sessions\/([^/?#]+)/);
  if (detail) {
    const [, rootId, sessionId] = detail;
    return `/graphs/${rootId}?branch=${sessionId}`;
  }
  if (path === "/today" || path.startsWith("/compare") || path.startsWith("/code-time")) {
    return "/sessions";
  }
  return path;
}

export function sourceUrl(kind: DatahubSourceKind, rawPath: string): string {
  const path = kind === "remote" ? remotePath(rawPath) : safeInternalPath(rawPath);
  return new URL(path, `${configuredOrigin(kind)}/`).toString();
}

export function currentAppPath(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}
