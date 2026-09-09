import type {
  CodeTimeCalibrationPayload,
  CodeTimeForecastsPayload,
  CodeTimeReport,
  ContextWindowPayload,
  DatahubChanges,
  DatahubSnapshot,
  CalibrationCohort as GeneratedCalibrationCohort,
  EstimateForecastRecord,
  ForecastKind,
  GraphStatsSession,
  GraphUsageSession,
  ModelUsagePayload,
  OverviewPayload,
  PatternMetrics,
  ProjectsPayload,
  RefreshPayload,
  SessionEventDetailsPayload,
  SessionEvidenceTimelinePayload,
  SessionGraphPayload,
  SessionItemDetailsPayload,
  SessionPage,
  SessionTreePayload,
  TodayPayload,
  TokenEfficiencyProjectPayload,
} from "./api/generated/datahub-api";

export type * from "./api/generated/datahub-api";

export type CursorRequest = {
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
};

export type TokenEfficiencyGrain = "daily" | "weekly";
export type TokenEfficiencyUnit = "session" | "turn";
export type TokenEfficiencyDistribution = import("./api/generated/datahub-api").Distribution;
export type TokenEfficiencyPeriodSummary = import("./api/generated/datahub-api").PeriodSummary;
export type TokenEfficiencyDelta = import("./api/generated/datahub-api").ComparisonDelta;
export type TokenEfficiencyPeriodComparison = import("./api/generated/datahub-api").PeriodComparison;
export type TokenEfficiencyPatternMetrics = PatternMetrics;
export type TokenEfficiencyPatternRow = import("./api/generated/datahub-api").PatternRow;
export type TokenEfficiencyContributor = import("./api/generated/datahub-api").Contributor;
export type TokenEfficiencyHotspotRow = import("./api/generated/datahub-api").HotspotRow;
export type TokenEfficiencyOutlierRow = import("./api/generated/datahub-api").OutlierRow;
export type TokenEfficiencyCoverage = import("./api/generated/datahub-api").Coverage3;

export type TimelineKind = import("./api/generated/datahub-api").TimelineEntry["kind"];
export type TimelineArtifactKind = NonNullable<
  import("./api/generated/datahub-api").TimelineEntry["artifact_kind"]
>;
export type SessionTimelineEntry = import("./api/generated/datahub-api").TimelineEntry;
export type CodeTimeWindow = CodeTimeReport["window"];
export type ForecastRecord = EstimateForecastRecord;
export type CalibrationCohort = GeneratedCalibrationCohort;

export async function fetchOverview(params?: { sinceDays?: number }) {
  const search = new URLSearchParams();
  if (params?.sinceDays != null) search.set("since_days", String(params.sinceDays));
  const query = search.toString();
  return fetchJson<OverviewPayload>(`/api/overview${query ? `?${query}` : ""}`);
}

export async function fetchToday() {
  return fetchJson<TodayPayload>("/api/today");
}

export async function fetchProjects() {
  return fetchJson<ProjectsPayload>("/api/projects");
}

export async function fetchSessions(
  request: CursorRequest & {
    sinceDays?: number;
    projectName?: string;
    agentVendor?: string;
  } = {},
) {
  const params = new URLSearchParams();
  if (request.sinceDays != null) params.set("since_days", String(request.sinceDays));
  if (request.projectName) params.set("project_name", request.projectName);
  if (request.agentVendor) params.set("agent_vendor", request.agentVendor);
  if (request.cursor) params.set("cursor", request.cursor);
  if (request.limit != null) params.set("limit", String(request.limit));
  const query = params.toString();
  return fetchJson<SessionPage>(`/api/sessions${query ? `?${query}` : ""}`, {
    signal: request.signal,
  });
}

export async function fetchContextWindow(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<ContextWindowPayload>(`/api/sessions/context-window?${params}`);
}

export async function fetchSessionGraph(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionGraphPayload>(`/api/sessions/graph?${params}`);
}

export async function fetchSessionTree(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionTreePayload>(`/api/sessions/tree?${params}`);
}

export async function fetchSessionEvidenceTimeline(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionEvidenceTimelinePayload>(
    `/api/sessions/evidence-timeline?${params}`,
  );
}

export async function fetchSessionItemDetails(itemIds: string[]) {
  const params = new URLSearchParams({ item_ids: itemIds.join(",") });
  return fetchJson<SessionItemDetailsPayload>(`/api/sessions/items?${params}`);
}

export async function fetchSessionEventDetails(eventIds: string[]) {
  const params = new URLSearchParams({ event_ids: eventIds.join(",") });
  return fetchJson<SessionEventDetailsPayload>(`/api/sessions/events?${params}`);
}

export async function fetchDatahubSnapshot(signal?: AbortSignal) {
  return fetchJson<DatahubSnapshot>("/api/datahub/snapshot", { signal });
}

export async function fetchDatahubChanges(params: {
  afterRevision: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams({ after_revision: String(params.afterRevision) });
  return fetchJson<DatahubChanges>(`/api/datahub/changes?${search}`, {
    signal: params.signal,
  });
}

export async function fetchModelUsage(params: {
  sinceDays?: number;
  projectName?: string | null;
  modelKey?: string | null;
  detail?: "sessions" | "turns" | "both";
  cursor?: string;
  revision?: number;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  search.set("since_days", String(params.sinceDays ?? 7));
  if (params.projectName) search.set("project_name", params.projectName);
  if (params.modelKey) search.set("model_key", params.modelKey);
  if (params.detail) search.set("detail", params.detail);
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.revision != null) search.set("revision", String(params.revision));
  search.set("limit", String(params.limit ?? 50));
  return fetchJson<ModelUsagePayload>(`/api/model-usage?${search}`, {
    signal: params.signal,
  });
}

export async function fetchTokenEfficiencyProject(params: {
  projectName: string;
  sinceDays?: number;
  detail?: "patterns" | "hotspots" | "outliers";
  grain?: TokenEfficiencyGrain;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams({
    project_name: params.projectName,
    since_days: String(Math.min(params.sinceDays ?? 7, 30)),
    limit: String(params.limit ?? 50),
  });
  if (params.detail) search.set("detail", params.detail);
  if (params.grain) search.set("grain", params.grain);
  if (params.cursor) search.set("cursor", params.cursor);
  return fetchJson<TokenEfficiencyProjectPayload>(
    `/api/token-efficiency/project?${search}`,
    { signal: params.signal },
  );
}

export async function refreshDatahubData() {
  return fetchJson<RefreshPayload>("/api/refresh", { method: "POST" });
}

export async function fetchCodeTimeReport(params?: {
  window?: CodeTimeWindow;
  project?: string;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  if (params?.window) search.set("window", params.window);
  if (params?.project) search.set("project", params.project);
  const query = search.toString();
  return fetchJson<CodeTimeReport>(`/api/code-time/report${query ? `?${query}` : ""}`, {
    signal: params?.signal,
  });
}

export async function fetchCodeTimeForecasts(params?: {
  kind?: ForecastKind;
  project?: string;
  targetHarnessName?: string;
  status?: string;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  if (params?.kind) search.set("kind", params.kind);
  if (params?.project) search.set("project", params.project);
  if (params?.targetHarnessName) search.set("target_harness_name", params.targetHarnessName);
  if (params?.status) search.set("status", params.status);
  if (params?.limit != null) search.set("limit", String(params.limit));
  const query = search.toString();
  return fetchJson<CodeTimeForecastsPayload>(
    `/api/code-time/forecasts${query ? `?${query}` : ""}`,
    { signal: params?.signal },
  );
}

export async function fetchCodeTimeCalibration(params?: {
  kind?: ForecastKind;
  project?: string;
  targetHarnessName?: string;
  targetModel?: string;
  estimatorModel?: string;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  if (params?.kind) search.set("kind", params.kind);
  if (params?.project) search.set("project", params.project);
  if (params?.targetHarnessName) search.set("target_harness_name", params.targetHarnessName);
  if (params?.targetModel) search.set("target_model", params.targetModel);
  if (params?.estimatorModel) search.set("estimator_model", params.estimatorModel);
  const query = search.toString();
  return fetchJson<CodeTimeCalibrationPayload>(
    `/api/code-time/calibration${query ? `?${query}` : ""}`,
    { signal: params?.signal },
  );
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.text();
  let payload: T | { error?: { message?: string } } | undefined;
  if (body) {
    try {
      payload = JSON.parse(body) as T | { error?: { message?: string } };
    } catch {
      if (response.ok) throw new Error("Datahub returned an invalid JSON response.");
    }
  }
  if (!response.ok) {
    const message =
      typeof payload === "object" &&
      payload !== null &&
      "error" in payload &&
      payload.error?.message
        ? payload.error.message
        : `Request failed: ${response.status}`;
    throw new Error(message);
  }
  if (payload === undefined) throw new Error("Datahub returned an empty response.");
  return payload as T;
}
