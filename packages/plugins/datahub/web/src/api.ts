import type {
  CostEvidence,
  CursorPageMetadata,
  OverviewPayload,
  ProjectItem,
  ProjectsPayload,
  SessionPage,
  TodayPayload,
} from "./api/generated/datahub-api";

export type * from "./api/generated/datahub-api";

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

export type CacheBreakRecord = {
  turn_id: string;
  type: "ttl_confirmed" | "ttl_likely" | "effort_switch" | "unattributed";
  idle_seconds: number;
  re_read_tokens: number;
  cached_after_tokens: number | null;
  est_cost_usd: number | null;
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
  execution_seconds: number;
  wait_seconds: number;
  runtime_available: boolean;
  mixed_models: boolean;
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
  revision: number;
  generated_at: string;
  cohort: { since_days: number; session_graph_count: number; turn_count: number };
  coverage: { total_models: number; missing_pricing: number };
  filters: { since_days: number; project_name: string | null; model_key: string | null };
  project_options: ProjectItem[];
  model_options: ModelUsageOption[];
  summary: {
    sessions: number;
    turns: number;
    models: number;
    processed_tokens: number;
    total_elapsed_seconds: number;
    total_execution_seconds: number;
    total_wait_seconds: number;
    runtime_eligible: number;
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

// Graph payloads mirror `ct session graph overview|stats|usage` with the datahub's
// retained parameter shapes (no narrative, no flat turn list).
export type GraphOrchestration = {
  kind?: string;
  vendors?: string[];
  session_count?: number;
  spawned_agent_count?: number;
  multi_agent_versions?: string[];
  multi_agent_modes?: string[];
  edge_counts?: Record<string, number>;
  agent_paths?: string[];
};

export type GraphSessionNode = {
  session_id: string;
  parent_session_id?: string | null;
  edge_type?: string | null;
  vendor?: string;
  model?: string | null;
  reasoning_effort?: string | null;
  status?: string;
  title?: string | null;
  agent_name?: string | null;
  agent_path?: string | null;
  cwd?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  multi_agent_version?: string | null;
  multi_agent_mode?: string | null;
};

export type GraphEdge = {
  type?: string | null;
  source_session_id?: string | null;
  target_session_id?: string | null;
  provenance?: string | null;
  confidence?: string | null;
};

export type GraphOverviewPayload = {
  graph_id: string;
  root_session_id: string;
  project?: string | null;
  graph?: { orchestration?: GraphOrchestration };
  summary?: {
    session_count?: number;
    turn_count?: number;
    started_at?: string | null;
    ended_at?: string | null;
    vendors?: string[];
  } | null;
  sessions: GraphSessionNode[];
  edges: GraphEdge[];
};

export type GraphStatsSession = {
  session_id: string;
  role?: string | null;
  vendor?: string | null;
  context_window?: { used_tokens?: number | null; used_percent?: number | null };
  runtime?: { turns?: number; execution_seconds?: number };
  usage?: { processed_tokens?: number; cached_prompt_tokens?: number };
};

export type GraphUsageSession = {
  session_id: string;
  parent_session_id?: string | null;
  role?: string | null;
  relationship?: string | null;
  title?: string | null;
  agent_name?: string | null;
  total_usage?: { processed_tokens?: number };
  estimated_cost?: CostEvidence | null;
  runtime?: { turns?: number };
};

export type SessionGraphPayload = {
  root_session_id: string;
  overview: GraphOverviewPayload;
  stats: {
    scope?: string | null;
    context_window?: { used_tokens?: number | null; used_percent?: number | null };
    usage?: {
      processed_tokens?: number;
      prompt_tokens?: number;
      cached_prompt_tokens?: number;
      completion_tokens?: number;
    };
    runtime?: { turns?: number; execution_seconds?: number; tool_calls?: number };
    sessions?: GraphStatsSession[] | null;
  };
  usage: {
    scope?: string | null;
    total_usage?: {
      processed_tokens?: number;
      prompt_tokens?: number;
      cached_prompt_tokens?: number;
      completion_tokens?: number;
      reasoning_tokens?: number;
    };
    runtime?: { turns?: number };
    estimated_cost?: CostEvidence | null;
    models?: Array<{
      provider?: string;
      model?: string;
      turns?: number;
      usage?: { processed_tokens?: number };
    }>;
    sessions?: GraphUsageSession[] | null;
  };
};

export async function fetchSessionGraph(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionGraphPayload>(`/api/sessions/graph?${params}`);
}

export type ConversationBranch = {
  session_id: string;
  parent_session_id?: string | null;
  source_turn_id?: string | null;
  vendor?: string;
  status?: string;
  title?: string | null;
  agent_name?: string | null;
  cwd?: string | null;
  started_at?: string | null;
  turn_count?: number;
  graph_session_count?: number;
  spawned_agent_count?: number;
};

export type SessionTreePayload = {
  root_session_id: string;
  selected_branch_id?: string;
  branches: ConversationBranch[];
};

export async function fetchSessionTree(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionTreePayload>(`/api/sessions/tree?${params}`);
}

export type TimelineKind = "user" | "assistant" | "tool" | "subagent" | "compaction";
export type TimelineArtifactKind = "file" | "command" | "check" | "commit" | "link";

export type TimelineTurnAccounting = {
  processed_tokens: number;
  cost_usd: number | null;
  cost_confidence: "reported" | "estimated" | null;
  execution_seconds: number | null;
  model_active_seconds: number | null;
  wait_before_seconds: number | null;
  provider: string | null;
  model: string | null;
};

export type SessionTimelineEntry = {
  id: string;
  timestamp: string | null;
  ended_at: string | null;
  session_id: string;
  turn_id: string;
  turn_sequence: number;
  position: number;
  vendor: string | null;
  agent_name: string | null;
  kind: TimelineKind;
  label: string;
  summary: string | null;
  status: string | null;
  failed: boolean;
  artifact_kind: TimelineArtifactKind | null;
  turn_accounting: TimelineTurnAccounting | null;
  item_ids: string[];
  event_ids: string[];
  target_session_id: string | null;
};

export type SessionEvidenceTimelinePayload = {
  schema_version: 1;
  revision: number;
  root_session_id: string;
  entrypoint_session_id: string;
  entries: SessionTimelineEntry[];
  warnings: string[];
};

export type SessionItemDetail = {
  item_id: string;
  session_id: string;
  turn_id: string;
  kind: string;
  type: string;
  operations?: string[];
  shape?: Record<string, unknown>;
  event_ids?: string[];
};

export type SessionEventDetail = {
  event_id: string;
  session_id: string;
  timestamp: string;
  type: string;
  tool_call?: Record<string, unknown>;
  llm?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  text?: { text: string };
};

export async function fetchSessionEvidenceTimeline(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<SessionEvidenceTimelinePayload>(`/api/sessions/evidence-timeline?${params}`);
}

export async function fetchSessionItemDetails(itemIds: string[]) {
  const params = new URLSearchParams({ item_ids: itemIds.join(",") });
  return fetchJson<SessionItemDetail[]>(`/api/sessions/items?${params}`);
}

export async function fetchSessionEventDetails(eventIds: string[]) {
  const params = new URLSearchParams({ event_ids: eventIds.join(",") });
  return fetchJson<{ root_session_id: string | null; matches: SessionEventDetail[] }>(
    `/api/sessions/events?${params}`,
  );
}

export type DatahubFreshness = {
  last_refresh_at: string | null;
  lag_seconds: number | null;
};

export type DatahubSourceStatus = {
  ready: number;
  ingesting: number;
  failed: number;
  incomplete: number;
};

export type DatahubSnapshot = {
  revision: number;
  generated_at: string;
  freshness: DatahubFreshness;
  catching_up: boolean;
  source_status: DatahubSourceStatus;
  minimum_available_revision: number;
  bootstrap: {
    ready: boolean;
    scan_started_at: string | null;
    scan_finished_at: string | null;
    error: string | null;
    last_result?: Record<string, unknown> | null;
  };
};

export type DatahubUpsert = {
  entity_type: string;
  entity_id: string;
  revision: number;
  payload: unknown;
};

export type DatahubDeletion = {
  entity_type: string;
  entity_id: string;
  revision: number;
};

export type DatahubChanges = {
  from_revision: number;
  to_revision: number;
  reset_required: boolean;
  upserts: DatahubUpsert[];
  deletions: DatahubDeletion[];
  invalidations: string[];
  freshness: DatahubFreshness;
  catching_up: boolean;
  source_status: DatahubSourceStatus;
};

export async function fetchDatahubSnapshot(signal?: AbortSignal) {
  return fetchJson<DatahubSnapshot>("/api/datahub/snapshot", { signal });
}

export async function fetchDatahubChanges(params: {
  afterRevision: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams({ after_revision: String(params.afterRevision) });
  return fetchJson<DatahubChanges>(`/api/datahub/changes?${search}`, { signal: params.signal });
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
  return fetchJson<{ status: "refreshed" }>("/api/refresh", { method: "POST" });
}

// ---------------------------------------------------------------------------
// Code Time — coding-time report and agent temporality forecasts
// ---------------------------------------------------------------------------

export type CodeTimeWindow = "today" | "72h" | "7d" | "30d";

export type CodeTimeTokens = {
  prompt_tokens: number;
  cached_prompt_tokens: number;
  cache_write_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  processed_tokens: number;
};

export type CodeTimeSession = {
  root_session_id: string;
  title: string | null;
  vendor: string;
  execution_seconds: number;
  wait_seconds: number;
  turns: number;
  tool_calls: number;
  tokens: CodeTimeTokens;
  cost_usd: number | null;
};

export type CodeTimeProject = {
  project_name: string;
  session_count: number;
  execution_seconds: number;
  wait_seconds: number;
  turns: number;
  tool_calls: number;
  tokens: CodeTimeTokens;
  cost_usd: number | null;
  sessions: CodeTimeSession[];
};

export type CodeTimeReport = {
  window: CodeTimeWindow;
  generated_at: string;
  totals: {
    session_count: number;
    project_count: number;
    execution_seconds: number;
    wait_seconds: number;
    turns: number;
    tool_calls: number;
    tokens: CodeTimeTokens;
    cost_usd: number | null;
  };
  projects: CodeTimeProject[];
};

export type ForecastKind =
  | "prospective"
  | "prospective_unbound"
  | "historical_backcast"
  | "runtime_advisory";

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
  target: {
    agent_vendor?: string;
    harness_name?: string;
    harness_version?: string;
    model?: string;
    effort?: string;
    execution_policy_fingerprint?: string;
  };
  estimator: {
    provider: string;
    model?: string;
    effort?: string;
    prompt_version: string;
    schema_version: string;
  };
  p50_minutes?: number;
  p80_minutes?: number;
  comparison?: {
    compared_at: string;
    actual_execution_seconds?: number;
    duration_bucket?: string;
    outcome: string;
    exclusion?: string;
  };
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
  statistics: {
    sample_count: number;
    calibration_ratio?: { value: number | "undefined"; interval_95?: [number, number]; reason?: string };
    median_absolute_log_error?: number | "undefined";
    within_1_5x_share?: number | "undefined";
    p80_coverage?: number | "undefined";
    compression_exponent?: { value: number | "undefined"; reason?: string };
  };
  buckets: Array<{
    bucket: string;
    sample_count: number;
    calibration_ratio?: number;
    within_1_5x_share?: number;
    outcome?: string;
  }>;
};

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
  return fetchJson<{ items: ForecastRecord[] }>(
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
  return fetchJson<{
    policy: { version?: string; min_samples?: number; within_factor?: number };
    cohorts: CalibrationCohort[];
  }>(`/api/code-time/calibration${query ? `?${query}` : ""}`, { signal: params?.signal });
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.text();
  let payload: T | { error?: { message?: string } } | undefined;
  if (body) {
    try {
      payload = JSON.parse(body) as T | { error?: { message?: string } };
    } catch {
      if (response.ok) {
        throw new Error("Datahub returned an invalid JSON response.");
      }
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
  if (payload === undefined) {
    throw new Error("Datahub returned an empty response.");
  }
  return payload as T;
}
