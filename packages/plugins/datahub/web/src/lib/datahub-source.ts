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

const SHARED_CAPABILITIES = new Set<DatahubCapability>([
  "sessions",
  "graphs",
]);

const DEFAULT_LOCAL_ORIGIN = "http://127.0.0.1:8765";
const DEFAULT_REMOTE_ORIGIN =
  "https://coding-trajectory-datahub-live.liux4989.workers.dev";

export function sourceProfile(snapshot: DatahubSnapshot): DatahubSourceProfile {
  return snapshot.transport?.source === "remote"
    ? {
        kind: "remote",
        label: "Shared",
        detail: "Live committed workspace data",
        capabilities: SHARED_CAPABILITIES,
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
  return profile?.capabilities.has(capability) ?? false;
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
