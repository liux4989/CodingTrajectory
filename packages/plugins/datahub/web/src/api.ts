import type {
  CodeTimeCalibrationPayload, CodeTimeForecastsPayload, CodeTimeReport,
  ContextWindowPayload, DatahubChanges, DatahubSnapshot,
  CalibrationCohort as GeneratedCalibrationCohort, EstimateForecastRecord,
  ForecastKind, GraphStatsSession, GraphUsageSession, ModelUsagePayload,
  OverviewPayload, PatternMetrics, ProjectsPayload, RefreshPayload,
  SessionEventDetailsPayload, SessionEvidenceTimelinePayload, SessionGraphPayload,
  SessionItemDetailsPayload, SessionPage, SessionTreePayload, TodayPayload,
  TokenEfficiencyProjectPayload,
} from "./api/generated/datahub-api";

export type * from "./api/generated/datahub-api";

export type CursorRequest = { cursor?: string; limit?: number; signal?: AbortSignal };
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
export type TimelineArtifactKind = NonNullable<import("./api/generated/datahub-api").TimelineEntry["artifact_kind"]>;
export type SessionTimelineEntry = import("./api/generated/datahub-api").TimelineEntry;
export type CodeTimeWindow = CodeTimeReport["window"];
export type ForecastRecord = EstimateForecastRecord;
export type CalibrationCohort = GeneratedCalibrationCohort;

type QueryParams = Record<string, string | number | boolean | string[] | null>;
type QueryEnvelope<T> = {
  protocol: "ct.datahub.v1";
  id: string | null;
  method: string;
  ok: boolean;
  data: T | null;
  availability: { state: "complete" | "partial" | "unavailable" | "unsupported"; missing: Array<{ field: string; reason: string }> };
  error: { code: string; message: string } | null;
};

export function fetchOverview(params?: { sinceDays?: number }) {
  return queryDatahub<OverviewPayload>("overview", { since_days: params?.sinceDays ?? null });
}

export function fetchToday() { return queryDatahub<TodayPayload>("today"); }
export function fetchProjects() { return queryDatahub<ProjectsPayload>("projects"); }

export function fetchSessions(request: CursorRequest & { sinceDays?: number; projectName?: string; agentVendor?: string } = {}) {
  return queryDatahub<SessionPage>("sessions", {
    since_days: request.sinceDays ?? null,
    project_name: request.projectName ?? null,
    agent_vendor: request.agentVendor ?? null,
    cursor: request.cursor ?? null,
    limit: request.limit ?? null,
  }, request.signal);
}

export function fetchContextWindow(sessionId: string) {
  return queryDatahub<ContextWindowPayload>("session.context-window", { session_id: sessionId, turn_id: null });
}
export function fetchSessionGraph(sessionId: string) {
  return queryDatahub<SessionGraphPayload>("session.graph", { session_id: sessionId });
}
export function fetchSessionTree(sessionId: string) {
  return queryDatahub<SessionTreePayload>("session.tree", { session_id: sessionId });
}
export function fetchSessionEvidenceTimeline(sessionId: string) {
  return queryDatahub<SessionEvidenceTimelinePayload>("session.evidence-timeline", { session_id: sessionId });
}
export function fetchSessionItemDetails(itemIds: string[]) {
  return queryDatahub<SessionItemDetailsPayload>("session.items", { item_ids: itemIds, include_content: false, turn_id: null });
}
export function fetchSessionEventDetails(eventIds: string[]) {
  return queryDatahub<SessionEventDetailsPayload>("session.events", { event_ids: eventIds, turn_id: null, type: null });
}
export function fetchDatahubSnapshot(signal?: AbortSignal) {
  return queryDatahub<DatahubSnapshot>("datahub.snapshot", {}, signal);
}
export function fetchDatahubChanges(params: { afterRevision: number; signal?: AbortSignal }) {
  return queryDatahub<DatahubChanges>("datahub.changes", { after_revision: params.afterRevision }, params.signal);
}

export function fetchModelUsage(params: { sinceDays?: number; projectName?: string | null; modelKey?: string | null; detail?: "sessions" | "turns" | "both"; cursor?: string; revision?: number; limit?: number; signal?: AbortSignal }) {
  return queryDatahub<ModelUsagePayload>("model-usage", {
    since_days: params.sinceDays ?? 7, project_name: params.projectName ?? null,
    model_key: params.modelKey ?? null, detail: params.detail ?? null,
    cursor: params.cursor ?? null, revision: params.revision ?? null, limit: params.limit ?? 50,
  }, params.signal);
}

export function fetchTokenEfficiencyProject(params: { projectName: string; sinceDays?: number; detail?: "patterns" | "hotspots" | "outliers"; grain?: TokenEfficiencyGrain; cursor?: string; limit?: number; signal?: AbortSignal }) {
  return queryDatahub<TokenEfficiencyProjectPayload>("token-efficiency.project", {
    project_name: params.projectName, since_days: Math.min(params.sinceDays ?? 7, 30),
    detail: params.detail ?? null, grain: params.grain ?? null,
    cursor: params.cursor ?? null, limit: params.limit ?? 50,
  }, params.signal);
}

export function refreshDatahubData() { return queryDatahub<RefreshPayload>("datahub.refresh"); }

export function fetchCodeTimeReport(params?: { window?: CodeTimeWindow; project?: string; signal?: AbortSignal }) {
  return queryDatahub<CodeTimeReport>("code-time.report", {
    window: params?.window ?? null, project: params?.project ?? null, agent_vendor: null,
  }, params?.signal);
}

export function fetchCodeTimeForecasts(params?: { kind?: ForecastKind; project?: string; targetHarnessName?: string; status?: string; limit?: number; signal?: AbortSignal }) {
  return queryDatahub<CodeTimeForecastsPayload>("code-time.forecasts", {
    kind: params?.kind ?? null, project: params?.project ?? null,
    target_harness_name: params?.targetHarnessName ?? null,
    status: params?.status ?? null, limit: params?.limit ?? null,
  }, params?.signal);
}

export function fetchCodeTimeCalibration(params?: { kind?: ForecastKind; project?: string; targetHarnessName?: string; targetModel?: string; estimatorModel?: string; signal?: AbortSignal }) {
  return queryDatahub<CodeTimeCalibrationPayload>("code-time.calibration", {
    kind: params?.kind ?? null, project: params?.project ?? null,
    target_harness_name: params?.targetHarnessName ?? null,
    target_model: params?.targetModel ?? null, estimator_model: params?.estimatorModel ?? null,
  }, params?.signal);
}

async function queryDatahub<T>(method: string, params: QueryParams = {}, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api/datahub/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol: "ct.datahub.v1", id: null, method, params }),
    signal,
  });
  const body = await response.text();
  let payload: QueryEnvelope<T> | { error?: { message?: string } } | undefined;
  if (body) {
    try { payload = JSON.parse(body) as QueryEnvelope<T>; }
    catch { if (response.ok) throw new Error("Datahub returned an invalid JSON response."); }
  }
  if (!response.ok || !payload || !("ok" in payload) || !payload.ok || payload.data === null) {
    const message = payload && "error" in payload && payload.error?.message
      ? payload.error.message : `Request failed: ${response.status}`;
    throw new Error(message);
  }
  return payload.data;
}
