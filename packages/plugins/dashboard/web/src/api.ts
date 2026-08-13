export type OverviewPayload = {
  projects: { count: number; vendors: Record<string, number> };
  sessions: {
    count: number;
    window_days: number;
    runtime: {
      execution_seconds: number;
      wait_seconds: number;
      turns: number;
      tool_calls: number;
      failed_tool_calls: number;
    };
    usage: {
      processed_tokens: number;
      cost_usd: number;
      known_cost_count: number;
      missing_cost_count: number;
    };
    top_projects: Array<{
      project: string;
      count: number;
      vendors: Record<string, number>;
      execution_seconds: number;
      processed_tokens: number;
      cost_usd: number;
      known_cost_count: number;
    }>;
    top_sessions: Array<{
      id?: string | null;
      title?: string | null;
      project?: string | null;
      vendor: string;
      vendors: string[];
      started_at?: string | null;
      execution_seconds: number;
      wait_seconds: number;
      turns: number;
      tool_calls: number;
      failed_tool_calls: number;
      processed_tokens: number;
    }>;
    warnings: Array<{ session_id?: string | null; project: string; message: string }>;
    errors: unknown[];
  };
};

export type ProjectItem = {
  name: string;
  path: string | null;
  vendors: string[];
};

export type SessionItem = {
  root_session_id: string;
  graph_id?: string | null;
  vendors: string[];
  session_ids: string[];
  title?: string | null;
  project?: string | null;
};

export type CursorPageMetadata = {
  revision: number;
  next_cursor: string | null;
  has_more: boolean;
};

export type SessionPage = {
  items: SessionItem[];
  page?: CursorPageMetadata;
};

export type CursorRequest = {
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
};

export type TokenEvidence = {
  value: number;
  confidence: "exact_usage" | "exact_text" | "estimated_tokens" | "structural" | "unknown";
  source: string;
};

export type CostEvidence = {
  value_usd: number;
  confidence: "reported" | "estimated";
  source: string;
  effective_date: string | null;
};

export type ContextCategory = {
  id: string;
  category: string;
  source_key: string;
  label: string;
  tokens: TokenEvidence;
  percent: number | null;
};

export type ContextEvent = {
  id: string;
  group: "before_first_prompt" | "turn" | "post_turn";
  turn_id: string | null;
  category: string;
  label: string;
  summary: string | null;
  tokens: TokenEvidence | null;
  source: string;
  confidence: TokenEvidence["confidence"];
  detail_ref: Record<string, string>;
  terminal_visible: boolean;
};

export type CompactionEventRecord = {
  timestamp: string;
  // Provider-native mechanism: "eviction_boundary" (Claude Code) carries
  // pre/post/dropped/trigger; "context_compacted" (Codex) does not, so its
  // delta fields stay null and render without those columns.
  mechanism: string;
  trigger: string | null;
  pre_tokens: number | null;
  post_tokens: number | null;
  dropped_tokens: number | null;
};

export type CompactionSummary = {
  count: number;
  cumulative_dropped_tokens: number | null;
  events: CompactionEventRecord[];
};

// A measured reduction in cache-hit tokens across a turn boundary, classified
// only when a supported cause is observed.
// - ttl_confirmed: idle gap >= vendor TTL max (OpenAI >=600s, Anthropic >=300s)
// - ttl_likely: idle in the ambiguous OpenAI 300-600s band
// - effort_switch: an observed reasoning-effort change aligns with the loss.
export type CacheBreakRecord = {
  turn_id: string;
  type: "ttl_confirmed" | "ttl_likely" | "effort_switch";
  idle_seconds: number;
  re_read_tokens: number;
  cached_after_tokens: number | null;
  est_cost_usd: number | null;
  // Resolved effort levels for a confirmed effort_switch. effort_from is null
  // on Claude Code's first /effort switch (baseline unknown).
  effort_from: string | null;
  effort_to: string | null;
};

export type CacheBreakSummary = {
  count: number;
  total_re_read_tokens: number;
  estimated_waste_usd: number | null;
  by_type: Record<string, number>;
  events: CacheBreakRecord[];
};

// A cache break enriched with the session it landed on, for the aggregate
// cache-breaks page (cross-session collection).
export type AggregateCacheBreak = CacheBreakRecord & {
  session_id: string;
  project: string;
  vendor: string;
  title: string | null;
  started_at: string | null;
  turn_index: number | null;
  // Turn start (the cache-rebuild turn); day-bucketed for the time series.
  timestamp: string | null;
};

export type CacheBreakTypeCount = {
  effort_switch: number;
  ttl_confirmed: number;
  ttl_likely: number;
};

export type CacheBreakSessionRow = {
  session_id: string;
  project: string;
  vendor: string;
  title: string | null;
  started_at: string | null;
  breaks: number;
  re_read_tokens: number;
  waste_usd: number;
  has_waste: boolean;
  confirmed: number;
  by_type: Partial<CacheBreakTypeCount>;
};

export type CacheBreakGroupRow = {
  breaks: number;
  re_read_tokens: number;
  waste_usd: number;
  has_waste: boolean;
};

export type CacheBreaksPayload = {
  schema_version: 1;
  filters: { since_days: number; project_name: string | null };
  project_options: ProjectItem[];
  summary: {
    sessions_with_breaks: number;
    total_breaks: number;
    by_type: CacheBreakTypeCount;
    total_re_read_tokens: number;
    estimated_waste_usd: number | null;
    confirmed_effort_switches: number;
    affected_projects: number;
    avg_break_cost_usd: number | null;
  };
  top_sessions: CacheBreakSessionRow[];
  by_vendor: Array<CacheBreakGroupRow & { vendor: string }>;
  by_project: Array<CacheBreakGroupRow & { project: string }>;
  time_buckets: Array<CacheBreakGroupRow & { bucket: string; by_type: Partial<CacheBreakTypeCount> }>;
  breaks: AggregateCacheBreak[];
  warnings: string[];
  pages?: { breaks?: CursorPageMetadata };
};

export type ContextWindowPayload = {
  schema_version: 1;
  session_id: string;
  active_session_id: string;
  vendor: string;
  model: string | null;
  context_window_tokens: TokenEvidence | null;
  used_tokens: TokenEvidence | null;
  used_percent: number | null;
  token_cost: CostEvidence | null;
  categories: ContextCategory[];
  provider_usage_buckets: ContextCategory[];
  session_sections: Array<{
    session_id: string;
    role: string;
    label: string;
    relationship: string | null;
    parent_session_id: string | null;
    used_tokens: TokenEvidence | null;
    used_percent: number | null;
    token_cost: CostEvidence | null;
  }>;
  events: ContextEvent[];
  compaction: CompactionSummary | null;
  cache_breaks: CacheBreakSummary | null;
  warnings: string[];
};

export type SessionAnalysis = {
  schema_version: 5;
  session_id: string;
  generated_at: string;
  app_server_thread_id: string;
  app_server_turn_id: string | null;
  task_story: {
    initial_request: string | null;
    follow_up_requests: string[];
    phases: Array<{
      label: string;
      turn_ids: string[];
      summary: string;
    }>;
    touched_artifacts: string[];
    outcomes: string[];
  };
  usage_evidence: {
    processed_tokens: number;
    billed_prompt_tokens: number;
    billed_uncached_prompt_tokens: number;
    billed_cached_prompt_tokens: number;
    billed_cache_write_tokens: number;
    billed_completion_tokens: number;
    billed_reasoning_tokens: number;
    resident_context_tokens: number | null;
    context_window_tokens: number | null;
    resident_context_percent: number | null;
    high_billed_turns: Array<Record<string, unknown>>;
    context_composition: Array<{
      category: string;
      concept: string;
      source_key: string;
      label: string;
      tokens: number;
      percent: number | null;
      confidence: string;
      resident_estimated_cost_usd: number | null;
    }>;
    expensive_billed_items: Array<{
      item_id: string;
      turn_id: string;
      label: string;
      category: string;
      summary: string;
      processed_tokens: number;
      billed_estimated_cost_usd: number;
    }>;
  };
  tool_evidence: {
    total_requested_calls: number;
    total_result_calls: number;
    failed_result_calls: number;
    output_chars: number;
    buckets: Array<{
      key: string;
      label: string;
      judgment: "good" | "neutral" | "risky";
      calls: number;
      failed_calls: number;
      output_chars: number;
      call_share: number;
      output_share: number;
    }>;
    top_output_calls: Array<{
      bucket: string;
      tool: string;
      output_chars: number;
      failed: boolean;
      command: string;
      timestamp: string | null;
    }>;
  };
  findings: Array<{
    kind: "justified_expensive_work" | "avoidable_pattern" | "optimal_pattern" | "recommended_workflow";
    title: string;
    body: string;
    impact: string | null;
    evidence: Array<{
      kind: "context_category" | "tool_item" | "tool_bucket" | "turn";
      ref: string;
      label: string;
      detail: string;
      severity: "hint" | "warning";
    }>;
  }>;
};

export type UsageBuckets = {
  prompt_tokens?: number;
  cached_prompt_tokens?: number;
  cache_write_tokens?: number;
  completion_tokens?: number;
  reasoning_tokens?: number;
  processed_tokens?: number;
  prompt_completion_tokens?: number;
  reported_total_tokens?: number | null;
  total_confidence?: "reported_consistent" | "reported_missing" | "reported_inconsistent";
};

export type ModelUsageContext = {
  final_used_tokens?: number | null;
  max_used_tokens?: number | null;
  context_window_tokens?: number | null;
  final_used_percent?: number | null;
  max_used_percent?: number | null;
  source?: string | null;
  confidence?: string;
};

export type ModelUsagePricing = {
  confidence: "estimated" | "missing_price";
  source: string | null;
  effective_date: string | null;
  breakdown?: Record<string, number>;
};

export type DistributionStats = {
  count: number;
  avg: number;
  median: number;
  p90: number;
  p95: number;
  max: number;
};

export type SessionTurnDistributionStats = {
  session: DistributionStats;
  turn: DistributionStats;
};

export type ModelUsageTokenStats = SessionTurnDistributionStats & {
  buckets: Record<string, SessionTurnDistributionStats>;
};

export type ModelUsageModel = {
  provider: string | null;
  model: string | null;
  model_key: string;
  sessions: number;
  turns: number;
  usage: UsageBuckets;
  estimated_cost_usd: number;
  elapsed_seconds: number;
  avg_session_cost_usd: number;
  avg_turn_cost_usd: number;
  avg_session_elapsed_seconds: number;
  avg_turn_elapsed_seconds: number;
  token_stats: SessionTurnDistributionStats;
  cost_stats: SessionTurnDistributionStats;
  pricing: ModelUsagePricing;
};

export type ModelUsageOption = {
  provider: string | null;
  model: string | null;
  model_key: string;
  sessions: number;
  turns: number;
  usage: UsageBuckets;
  estimated_cost_usd: number;
  elapsed_seconds: number;
};

export type ModelUsageSession = {
  id: string;
  project: string | null;
  title: string | null;
  vendor: string | null;
  started_at: string | null;
  completed_at: string | null;
  elapsed_seconds: number;
  usage: UsageBuckets;
  context: ModelUsageContext | null;
  dominant_model: { provider: string | null; model: string | null; basis: string } | null;
  estimated_cost_usd: number;
  models: Array<
    Omit<
      ModelUsageModel,
      | "sessions"
      | "avg_session_cost_usd"
      | "avg_turn_cost_usd"
      | "avg_session_elapsed_seconds"
      | "avg_turn_elapsed_seconds"
      | "elapsed_seconds"
      | "token_stats"
      | "cost_stats"
    >
  >;
};

export type ModelUsageTurn = {
  session_id: string;
  turn_id: string;
  sequence: number;
  started_at: string | null;
  provider: string | null;
  model: string | null;
  model_key: string;
  project?: string | null;
  session_title?: string | null;
  vendor?: string | null;
  usage: UsageBuckets;
  context: ModelUsageContext | null;
  estimated_cost_usd: number;
  pricing: ModelUsagePricing;
};

export type ModelUsagePayload = {
  schema_version: 1;
  filters: { since_days: number; project_name: string | null; model_key: string | null };
  project_options: ProjectItem[];
  model_options: ModelUsageOption[];
  summary: {
    sessions: number;
    turns: number;
    models: number;
    processed_tokens: number;
    total_elapsed_seconds: number;
    avg_tokens_per_session: number;
    avg_tokens_per_turn: number;
    avg_elapsed_seconds_per_session: number;
    token_stats: ModelUsageTokenStats;
    cost_stats: SessionTurnDistributionStats;
    elapsed_stats: { session: DistributionStats };
    estimated_cost_usd: number;
    missing_price_count: number;
    top_model_by_cost: string | null;
    top_model_by_sessions: string | null;
  };
  models: ModelUsageModel[];
  sessions: ModelUsageSession[];
  turns: ModelUsageTurn[];
  time_buckets: Record<string, Array<{
    bucket: string;
    model_key: string;
    provider: string | null;
    model: string | null;
    turns: number;
    estimated_cost_usd: number;
    usage: UsageBuckets;
  }>>;
  warnings: Array<{ session_id: string; message: string }>;
  pages?: {
    sessions?: CursorPageMetadata;
    turns?: CursorPageMetadata;
  };
};

export type TokenEfficiencyGrain = "daily" | "weekly";
export type TokenEfficiencyUnit = "session" | "turn";

export type TokenEfficiencyDistribution = {
  count: number;
  avg: number;
  median: number;
  p90: number;
  p95: number;
  max: number;
};

export type TokenEfficiencyPeriodSummary = {
  bucket: string;
  label: string;
  is_complete: boolean;
  started_at: string;
  ended_at: string;
  session_count: number;
  turn_count: number;
  total_prompt_tokens: number;
  session_prompt: TokenEfficiencyDistribution;
  turn_prompt: TokenEfficiencyDistribution;
  pattern_prompt_tokens: number;
  pattern_share: number;
};

export type TokenEfficiencyDelta = {
  total_prompt_tokens_pct: number | null;
  session_median_pct: number | null;
  session_p90_pct: number | null;
  turn_median_pct: number | null;
  turn_p90_pct: number | null;
};

export type TokenEfficiencyPeriodComparison = {
  grain: TokenEfficiencyGrain;
  current: TokenEfficiencyPeriodSummary;
  previous: TokenEfficiencyPeriodSummary | null;
  deltas: TokenEfficiencyDelta;
};

export type TokenEfficiencyPatternMetrics = {
  incidence_count: number;
  incidence_rate: number;
  calls: number;
  total_prompt_tokens: number;
  token_share: number;
  zero_inclusive: {
    session: TokenEfficiencyDistribution;
    turn: TokenEfficiencyDistribution;
  };
  conditional: {
    session: TokenEfficiencyDistribution;
    turn: TokenEfficiencyDistribution;
  };
  indicators: {
    repeated_read: number;
    parallel_fanout: number;
    truncated_output: number;
  };
};

export type TokenEfficiencyPatternRow = {
  key: string;
  label: string;
  kind: "exclusive" | "indicator";
  current: TokenEfficiencyPatternMetrics;
  previous: TokenEfficiencyPatternMetrics | null;
  deltas: {
    prompt_tokens_pct: number | null;
    incidence_rate_points: number;
    calls_pct: number | null;
    session_median_pct?: number | null;
    session_p90_pct?: number | null;
    turn_median_pct?: number | null;
    turn_p90_pct?: number | null;
  };
  contributors: TokenEfficiencyContributor[];
};

export type TokenEfficiencyContributor = {
  session_id: string;
  turn_id: string | null;
  title: string | null;
  prompt_tokens: number;
  calls: number;
  repeated_calls?: number;
  pattern?: string | null;
};

export type TokenEfficiencyHotspotRow = {
  key: string;
  resource: string;
  status: string;
  sessions: number;
  turns: number;
  calls: number;
  repeat_count: number;
  enclosing_prompt_tokens: number;
  largest_call_tokens: number;
  largest_call_share: number;
  broad_calls: number;
  targeted_calls: number;
  previous_enclosing_prompt_tokens: number;
  delta_pct: number | null;
  session: TokenEfficiencyDistribution;
  turn: TokenEfficiencyDistribution;
  contributors: TokenEfficiencyContributor[];
};

export type TokenEfficiencyOutlierRow = {
  session_id: string;
  turn_id: string;
  title: string | null;
  completed_at: string | null;
  prompt_tokens: number;
  session_share: number;
  max_context_tokens: number | null;
  primary_pattern: string | null;
  reason_codes: string[];
};

export type TokenEfficiencyCoverage = {
  root_graphs?: number;
  sessions?: number;
  turns?: number;
  tool_items?: number;
  attributed_tool_items?: number;
  undated_tool_items?: number;
  truncated_input_summaries?: number;
  [key: string]: number | undefined;
};

export type TokenEfficiencyProjectIndexRow = {
  project_name: string;
  display_name: string;
  root_graphs: number;
  prompt_tokens: number;
  graph_prompt: TokenEfficiencyDistribution;
};

export type TokenEfficiencyIndexPayload = {
  schema_version: 1;
  generated_at: string;
  filters: { since_days: number };
  attribution: Record<string, unknown>;
  coverage: TokenEfficiencyCoverage;
  warnings: string[];
  project_options: ProjectItem[];
  projects: TokenEfficiencyProjectIndexRow[];
  pages?: { projects?: CursorPageMetadata };
};

export type TokenEfficiencyProjectPayload = {
  schema_version: 1;
  generated_at: string;
  filters: {
    since_days: number;
    discovery_days: number;
    project_name: string;
  };
  attribution: Record<string, unknown>;
  coverage: TokenEfficiencyCoverage;
  warnings: string[];
  project: {
    name: string;
    display_name: string;
    path?: string | null;
  };
  comparisons: {
    daily: TokenEfficiencyPeriodComparison | null;
    weekly: TokenEfficiencyPeriodComparison | null;
  };
  trends: {
    daily: TokenEfficiencyPeriodSummary[];
    weekly: TokenEfficiencyPeriodSummary[];
  };
  patterns: Record<TokenEfficiencyGrain, TokenEfficiencyPatternRow[]>;
  hotspots: Record<TokenEfficiencyGrain, TokenEfficiencyHotspotRow[]>;
  outliers: Record<TokenEfficiencyGrain, TokenEfficiencyOutlierRow[]>;
  pages?: Partial<
    Record<
      "patterns" | "hotspots" | "outliers",
      Partial<Record<TokenEfficiencyGrain, CursorPageMetadata>>
    >
  >;
};

export async function fetchOverview(params?: { sinceDays?: number }) {
  const search = new URLSearchParams();
  if (params?.sinceDays != null) search.set("since_days", String(params.sinceDays));
  const query = search.toString();
  const suffix = query ? `?${query}` : "";
  return fetchJson<OverviewPayload>(`/api/overview${suffix}`);
}

export async function fetchProjects() {
  return fetchJson<{ items: ProjectItem[] }>("/api/projects");
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

export async function analyzeSession(sessionId: string, refresh = false) {
  return fetchJson<JobAccepted>(`/api/sessions/${encodeURIComponent(sessionId)}/analysis`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
}

export type JobStatus = "pending" | "running" | "ready" | "error";

export type JobRecord = {
  id: string;
  kind: string;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  progress: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
};

export type DashboardFreshness = {
  last_refresh_at: string | null;
  lag_seconds: number | null;
};

export type DashboardSourceStatus = {
  ready: number;
  ingesting: number;
  failed: number;
  incomplete: number;
};

export type DashboardSnapshot = {
  revision: number;
  generated_at: string;
  freshness: DashboardFreshness;
  catching_up: boolean;
  source_status: DashboardSourceStatus;
  minimum_available_revision: number;
  bootstrap: {
    ready: boolean;
    scan_started_at: string | null;
    scan_finished_at: string | null;
    error: string | null;
    last_result?: Record<string, unknown> | null;
  };
};

export type DashboardUpsert = {
  entity_type: string;
  entity_id: string;
  revision: number;
  payload: unknown;
};

export type DashboardDeletion = {
  entity_type: string;
  entity_id: string;
  revision: number;
};

export type DashboardChanges = {
  from_revision: number;
  to_revision: number;
  reset_required: boolean;
  upserts: DashboardUpsert[];
  deletions: DashboardDeletion[];
  invalidations: string[];
  freshness: DashboardFreshness;
  catching_up: boolean;
  source_status: DashboardSourceStatus;
};

type JobAccepted = {
  status: "pending";
  job_id: string;
  operation_key?: string;
  reused?: boolean;
};

export async function fetchJobStatus(jobId: string, signal?: AbortSignal) {
  return fetchJson<JobRecord>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal });
}

export async function fetchDashboardSnapshot(signal?: AbortSignal) {
  return fetchJson<DashboardSnapshot>("/api/dashboard/snapshot", { signal });
}

export async function fetchDashboardChanges(params: {
  afterRevision: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams({ after_revision: String(params.afterRevision) });
  return fetchJson<DashboardChanges>(`/api/dashboard/changes?${search}`, { signal: params.signal });
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

export async function fetchTokenEfficiencyIndex(params: {
  sinceDays?: number;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams({
    since_days: String(Math.min(params.sinceDays ?? 7, 30)),
    limit: String(params.limit ?? 50),
  });
  if (params.cursor) search.set("cursor", params.cursor);
  return fetchJson<TokenEfficiencyIndexPayload>(`/api/token-efficiency?${search}`, {
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

export async function fetchCacheBreaks(params: {
  sinceDays?: number;
  projectName?: string | null;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  search.set("since_days", String(params.sinceDays ?? 7));
  if (params.projectName) search.set("project_name", params.projectName);
  if (params.cursor) search.set("cursor", params.cursor);
  search.set("limit", String(params.limit ?? 50));
  return fetchJson<CacheBreaksPayload>(`/api/cache-breaks?${search}`, {
    signal: params.signal,
  });
}

export async function refreshDashboardData() {
  return fetchJson<{ status: "refreshed" }>("/api/refresh", { method: "POST" });
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = (await response.json()) as T | { error?: { message?: string } };
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
  return payload as T;
}

async function waitForPoll(signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    throw new DOMException("Request aborted", "AbortError");
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Request aborted", "AbortError"));
    };
    const timeout = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, 1_500);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function startAndWaitForJob<T>(
  url: string,
  filters: Record<string, string | number | boolean>,
  signal?: AbortSignal,
): Promise<T> {
  const accepted = await fetchJson<JobAccepted>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filters }),
    signal,
  });
  while (true) {
    const job = await fetchJobStatus(accepted.job_id, signal);
    if (job.status === "ready") {
      if (!job.result) {
        throw new Error("Dashboard collection completed without a result");
      }
      return job.result as unknown as T;
    }
    if (job.status === "error") {
      throw new Error(job.error || "Dashboard collection failed");
    }
    await waitForPoll(signal);
  }
}
