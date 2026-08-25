export type TokenUsage = {
  prompt_tokens: number;
  cached_prompt_tokens: number;
  cache_write_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  processed_tokens: number;
};

export type SessionSlice = {
  root_session_id: string;
  title: string | null;
  vendor: string;
  execution_seconds: number;
  wait_seconds: number;
  turns: number;
  tool_calls: number;
  tokens: TokenUsage;
  cost_usd: number | null;
};

export type ProjectSlice = {
  project_name: string;
  session_count: number;
  execution_seconds: number;
  wait_seconds: number;
  turns: number;
  tool_calls: number;
  tokens: TokenUsage;
  cost_usd: number | null;
  sessions: SessionSlice[];
};

export type Totals = {
  session_count: number;
  project_count: number;
  execution_seconds: number;
  wait_seconds: number;
  turns: number;
  tool_calls: number;
  tokens: TokenUsage;
  cost_usd: number | null;
};

export type HourlyDensity = {
  hour: number;
  density: number;
  by_project: Record<string, number>;
};

export type ProjectDayTrend = {
  date: string;
  seconds: number;
};

export type ProjectTrend = {
  project_name: string;
  days: ProjectDayTrend[];
};

export type CodeTimeReport = {
  window: string;
  generated_at: string;
  totals: Totals;
  projects: ProjectSlice[];
  hourly_density?: HourlyDensity[];
  project_trend?: ProjectTrend[];
};

const BASE = "";

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export function fetchToday(params?: {
  window?: string;
  project?: string;
}): Promise<CodeTimeReport> {
  const search = new URLSearchParams();
  if (params?.window) search.set("window", params.window);
  if (params?.project) search.set("project", params.project);
  const qs = search.toString();
  return fetchJson<CodeTimeReport>(`/api/today${qs ? `?${qs}` : ""}`);
}

// ---------------------------------------------------------------------------
// estimate.* — agent temporality forecasts and calibration
// ---------------------------------------------------------------------------

export type ForecastKind =
  | "prospective"
  | "prospective_unbound"
  | "historical_backcast"
  | "runtime_advisory";

export type ForecastTarget = {
  agent_vendor?: string;
  harness_name?: string;
  harness_version?: string;
  model?: string;
  effort?: string;
  execution_policy_fingerprint?: string;
};

export type ForecastEstimator = {
  provider: string;
  model?: string;
  effort?: string;
  prompt_version: string;
  schema_version: string;
};

export type ForecastComparison = {
  compared_at: string;
  actual_execution_seconds?: number;
  duration_bucket?: string;
  outcome: string;
  exclusion?: string;
};

export type ForecastRecord = {
  prediction_id: string;
  forecast_kind: ForecastKind;
  role: "primary" | "diagnostic";
  status: "unbound" | "uncompared" | "compared";
  turn_id?: string;
  task_fingerprint: string;
  issued_at: string;
  bound_at?: string;
  project_name?: string;
  session_title?: string;
  target: ForecastTarget;
  estimator: ForecastEstimator;
  p50_minutes?: number;
  p80_minutes?: number;
  comparison?: ForecastComparison;
};

export type ForecastListResponse = {
  items: ForecastRecord[];
};

export type CalibrationBucket = {
  bucket: string;
  sample_count: number;
  calibration_ratio?: number;
  within_1_5x_share?: number;
  outcome?: string;
};

export type CalibrationStatistics = {
  sample_count: number;
  calibration_ratio?: { value: number | "undefined"; interval_95?: [number, number]; reason?: string };
  median_absolute_log_error?: number | "undefined";
  within_1_5x_share?: number | "undefined";
  p80_coverage?: number | "undefined";
  compression_exponent?: { value: number | "undefined"; reason?: string };
};

export type CalibrationCohort = {
  cohort: {
    forecast_kind: ForecastKind;
    estimator_provider?: string;
    estimator_model?: string;
    estimator_effort?: string;
    prompt_version?: string;
    schema_version?: string;
    retrieval_policy_version?: string;
  };
  eligible_count: number;
  primary_count: number;
  exclusions: Record<string, number>;
  statistics: CalibrationStatistics;
  buckets: CalibrationBucket[];
};

export type CalibrationResponse = {
  policy: { version?: string; min_samples?: number; within_factor?: number };
  cohorts: CalibrationCohort[];
};

export type ForecastFilters = {
  kind?: ForecastKind;
  project?: string;
  target_harness_name?: string;
  status?: string;
  limit?: number;
};

export type CalibrationFilters = {
  kind?: ForecastKind;
  project?: string;
  target_harness_name?: string;
  target_model?: string;
  estimator_model?: string;
};

export function fetchForecasts(params?: ForecastFilters): Promise<ForecastListResponse> {
  const search = new URLSearchParams();
  if (params?.kind) search.set("kind", params.kind);
  if (params?.project) search.set("project", params.project);
  if (params?.target_harness_name) search.set("target_harness_name", params.target_harness_name);
  if (params?.status) search.set("status", params.status);
  if (params?.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return fetchJson<ForecastListResponse>(`/api/forecasts${qs ? `?${qs}` : ""}`);
}

export function fetchCalibration(params?: CalibrationFilters): Promise<CalibrationResponse> {
  const search = new URLSearchParams();
  if (params?.kind) search.set("kind", params.kind);
  if (params?.project) search.set("project", params.project);
  if (params?.target_harness_name) search.set("target_harness_name", params.target_harness_name);
  if (params?.target_model) search.set("target_model", params.target_model);
  if (params?.estimator_model) search.set("estimator_model", params.estimator_model);
  const qs = search.toString();
  return fetchJson<CalibrationResponse>(`/api/calibration${qs ? `?${qs}` : ""}`);
}
