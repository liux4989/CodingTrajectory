import { request } from "./api";
import type { StrategyManifest } from "./generated/monitor.strategymanifest";
import type { Watch } from "./generated/monitor.watch";
import type { Evaluation } from "./generated/monitor.evaluation";
import type { Finding } from "./generated/monitor.finding";
import type { DryRunResult } from "./generated/monitor.dryrunresult";
import type { RefreshResult } from "./generated/monitor.refreshresult";

export type {
  StrategyManifest,
  Watch,
  Evaluation,
  Finding,
  DryRunResult,
  RefreshResult,
};

export type MonitorRoute =
  | { view: "strategies" }
  | { view: "activity" }
  | { view: "findings" }
  | { view: "watch"; watchId: string };

/** Monitor routes live in the hash namespace `#/monitor/...`. */
export function readMonitorRoute(): MonitorRoute | null {
  const hash = location.hash;
  if (!hash.startsWith("#/monitor")) return null;
  const parts = hash.slice(1).split("/").filter(Boolean);
  if (parts.length === 1) return { view: "activity" };
  if (parts[1] === "strategies") return { view: "strategies" };
  if (parts[1] === "findings") return { view: "findings" };
  if (parts[1] === "watches" && parts[2]) {
    return { view: "watch", watchId: parts[2] };
  }
  return { view: "activity" };
}

export interface WatchFormValue {
  strategy_id: string;
  name: string;
  scope: { project_name?: string; session_id?: string };
  config: {
    measure: string;
    threshold_tokens: number;
    severity: string;
    emit_findings: boolean;
  };
}

export const monitorApi = {
  strategies: (signal?: AbortSignal) =>
    request<{ items: StrategyManifest[] }>(
      "/api/monitor/strategies",
      undefined,
      signal,
    ).then((data) => data.items),

  watches: (signal?: AbortSignal) =>
    request<{ items: Watch[] }>("/api/monitor/watches", undefined, signal).then(
      (data) => data.items,
    ),

  watch: (id: string, signal?: AbortSignal) =>
    request<{ watch: Watch }>(
      `/api/monitor/watches/${id}`,
      undefined,
      signal,
    ).then((data) => data.watch),

  createWatch: (value: WatchFormValue) =>
    request<{ watch: Watch }>("/api/monitor/watches", value).then(
      (data) => data.watch,
    ),

  updateWatch: (
    id: string,
    update: Partial<WatchFormValue> & { enabled?: boolean },
  ) =>
    request<{ watch: Watch }>(`/api/monitor/watches/${id}`, update).then(
      (data) => data.watch,
    ),

  dryRun: (id: string, maxSessions = 8) =>
    request<{ run: DryRunResult }>(`/api/monitor/watches/${id}/dry-run`, {
      max_sessions: maxSessions,
    }).then((data) => data.run),

  refresh: (id: string, maxSessions = 32) =>
    request<{ run: RefreshResult }>(`/api/monitor/watches/${id}/refresh`, {
      max_sessions: maxSessions,
    }).then((data) => data.run),

  evaluations: (
    filter: {
      watch_id?: string;
      trigger?: string;
      state?: string;
      result?: string;
      limit?: number;
    },
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filter))
      if (value) params.set(key, String(value));
    const suffix = params.size ? `?${params}` : "";
    return request<{ items: Evaluation[] }>(
      `/api/monitor/evaluations${suffix}`,
      undefined,
      signal,
    ).then((data) => data.items);
  },

  findings: (
    filter: { status?: string; watch_id?: string },
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filter))
      if (value) params.set(key, String(value));
    const suffix = params.size ? `?${params}` : "";
    return request<{ items: Finding[] }>(
      `/api/monitor/findings${suffix}`,
      undefined,
      signal,
    ).then((data) => data.items);
  },

  setFindingStatus: (id: string, status: string) =>
    request<{ finding: Finding }>(`/api/monitor/findings/${id}/status`, {
      status,
    }).then((data) => data.finding),
};
