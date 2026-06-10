export type TokenUsage = {
  input_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
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
