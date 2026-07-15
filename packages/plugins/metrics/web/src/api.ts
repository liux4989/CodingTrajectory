export type MetricFormat = "integer" | "tokens" | "percent" | "usd" | "duration" | "ratio";

export type CohortSummary = {
  since_days: number;
  session_graph_count: number;
  turn_count: number;
  usage_eligible: number;
  pricing_eligible: number;
  runtime_eligible: number;
  cache_eligible: number;
  generated_at: string;
};

export type Highlight = {
  key: string;
  label: string;
  value: number | null;
  format: MetricFormat;
  detail: string;
};

export type ChartPoint = {
  key: string;
  label: string;
  primary: number;
  secondary: number | null;
  tertiary: number | null;
  sample_count: number;
};

export type ComparisonRow = {
  key: string;
  label: string;
  provider: string | null;
  model: string | null;
  graphs: number;
  turns: number;
  processed_tokens: number;
  cache_hit_rate: number | null;
  cost_usd: number | null;
  pricing_coverage: number;
  active_seconds: number;
  wait_seconds: number;
};

export type SessionRow = {
  session_graph_id: string;
  project: string | null;
  title: string | null;
  vendor: string | null;
  model_label: string;
  mixed_models: boolean;
  turns: number;
  processed_tokens: number | null;
  cost_usd: number | null;
  cost_confidence: string | null;
  active_seconds: number | null;
  wait_seconds: number | null;
};

export type CategoryResponse = {
  schema_version: 1;
  category: "tokens" | "cost" | "execution";
  chart: string;
  cohort: CohortSummary;
  highlights: Highlight[];
  chart_points: ChartPoint[];
  comparison_rows: ComparisonRow[];
  sessions: SessionRow[];
  warnings: string[];
};

export async function fetchCategory(endpoint: string, sinceDays: number, chart: string): Promise<CategoryResponse> {
  const query = new URLSearchParams({ since_days: String(sinceDays), chart });
  const response = await fetch(`${endpoint}?${query}`);
  const payload = await response.json() as CategoryResponse | { error?: { message?: string } };
  if (!response.ok) {
    throw new Error("error" in payload ? payload.error?.message ?? `Request failed (${response.status})` : `Request failed (${response.status})`);
  }
  return payload as CategoryResponse;
}
