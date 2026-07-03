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

export type ProjectDetail = {
  name: string;
  path: string | null;
  vendors: string[];
  since_days: number | null;
  sessions: SessionItem[];
  session_count: number;
};

export type SessionItem = {
  id?: string | null;
  root_session_id?: string | null;
  v?: string[];
  vendors?: string[];
  title?: string | null;
  project_name?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
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

export type ContextWindowPayload = {
  schema_version: 1;
  session_id: string;
  vendor: string;
  model: string | null;
  context_window_tokens: TokenEvidence | null;
  used_tokens: TokenEvidence | null;
  used_percent: number | null;
  token_cost: CostEvidence | null;
  categories: ContextCategory[];
  provider_usage_buckets: ContextCategory[];
  events: ContextEvent[];
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

export type AgentTurnResult = {
  schema_version: 2;
  generated_at: string;
  agent_session_id: string;
  app_server_turn_id: string | null;
  response_text: string;
};

export type AgentSession = {
  agent_session_id: string;
  route_scope: string | null;
  created_at: string;
  last_used_at: string;
  active_job_id: string | null;
  recent_job_ids: string[];
};

export type CleanupSummary = {
  candidate_count: number;
  skipped_count: number;
  skipped_reasons: Record<string, number>;
};

export type CleanupTarget = {
  path: string;
  reason: string[];
  project?: string;
  vendor?: string;
  session_id?: string | null;
  modified_at?: string | null;
  vendors?: string[];
};

export type CleanupPreview = {
  target_kind: "project" | "session";
  filters: Record<string, unknown>;
  summary: CleanupSummary;
  candidates: CleanupTarget[];
  skipped: Array<{ kind: string; path: string; reason: string[] }>;
};

export type CleanupApplyPayload = {
  action: "trash" | "delete";
  paths: string[];
  filters?: Record<string, unknown>;
};

export type CleanupResult = {
  action: string;
  manifest_path: string | null;
  summary: {
    target_count: number;
    candidate_count: number;
    skipped_count: number;
    error_count: number;
    skipped_reasons: Record<string, number>;
  };
  errors: Array<{ path: string; error: string }>;
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
};

export type ErrorCollectionKind =
  | "abort_coding_session"
  | "abrupt_coding_mid_session"
  | "fail_tool_coverage";

export type ErrorCollectionItem = {
  id: string;
  session_id: string;
  project: string | null;
  session_title: string | null;
  started_at: string | null;
  ended_at: string | null;
  kind: ErrorCollectionKind;
  severity: "info" | "warning" | "critical";
  confidence: "direct" | "inferred";
  title: string;
  detail: string;
  evidence: string[];
};

export type ErrorCollectionPayload = {
  schema_version: 1;
  filters: { since_days: number; project_name: string | null };
  project_options: ProjectItem[];
  summary: {
    sessions: number;
    affected_sessions: number;
    total_errors: number;
    by_kind: Record<ErrorCollectionKind, number>;
    by_severity: Record<"critical" | "warning" | "info", number>;
    top_projects: Array<{ project: string; errors: number }>;
    generated_at: string;
  };
  errors: ErrorCollectionItem[];
};

export async function fetchOverview() {
  return fetchJson<OverviewPayload>("/api/overview");
}

export async function fetchProjects() {
  return fetchJson<{ items: ProjectItem[] }>("/api/projects");
}

export async function fetchProjectDetail(projectName: string, sinceDays?: number) {
  const params = new URLSearchParams({ project_name: projectName });
  if (sinceDays != null) params.set("since_days", String(sinceDays));
  return fetchJson<ProjectDetail>(`/api/projects/detail?${params}`);
}

export async function fetchSessions() {
  return fetchJson<{ items: SessionItem[] }>("/api/sessions");
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

export async function createAgentSession(params: {
  routeScope?: string | null;
  ephemeral?: boolean;
}) {
  return fetchJson<AgentSession>("/api/agent-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      route_scope: params.routeScope ?? null,
      ephemeral: params.ephemeral ?? false,
    }),
  });
}

export async function fetchAgentSession(agentSessionId: string) {
  return fetchJson<AgentSession>(`/api/agent-sessions/${encodeURIComponent(agentSessionId)}`);
}

export async function closeAgentSession(agentSessionId: string) {
  return fetchJson<{ status: "closed"; agent_session_id: string }>(
    `/api/agent-sessions/${encodeURIComponent(agentSessionId)}`,
    { method: "DELETE" },
  );
}

export async function runAgentSessionTurn(params: {
  agentSessionId: string;
  prompt: string;
  outputSchema?: Record<string, unknown> | null;
}) {
  return fetchJson<JobAccepted>(
    `/api/agent-sessions/${encodeURIComponent(params.agentSessionId)}/turns`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: params.prompt,
        output_schema: params.outputSchema ?? null,
      }),
    },
  );
}

export async function runAgentTurn(params: {
  prompt: string;
  threadId?: string | null;
  outputSchema?: Record<string, unknown> | null;
  ephemeral?: boolean;
}) {
  return fetchJson<JobAccepted>("/api/agent-turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: params.prompt,
      thread_id: params.threadId ?? null,
      output_schema: params.outputSchema ?? null,
      ephemeral: params.ephemeral ?? false,
    }),
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

type JobAccepted = {
  status: "pending";
  job_id: string;
  operation_key?: string;
  reused?: boolean;
  agent_session_id?: string;
};

export async function fetchJobStatus(jobId: string) {
  return fetchJson<JobRecord>(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export async function fetchCleanupPreview(kind: "project" | "session") {
  return fetchJson<CleanupPreview>(`/api/cleanup/${kind}/preview`);
}

export async function fetchModelUsage(params: { sinceDays?: number; projectName?: string | null; modelKey?: string | null }) {
  const search = new URLSearchParams();
  search.set("since_days", String(params.sinceDays ?? 7));
  if (params.projectName) search.set("project_name", params.projectName);
  if (params.modelKey) search.set("model_key", params.modelKey);
  return fetchJson<ModelUsagePayload>(`/api/model-usage?${search}`);
}

export async function fetchErrorCollection(params: { sinceDays?: number; projectName?: string | null }) {
  const search = new URLSearchParams();
  search.set("since_days", String(params.sinceDays ?? 7));
  if (params.projectName) search.set("project_name", params.projectName);
  return fetchJson<ErrorCollectionPayload>(`/api/error-collection?${search}`);
}

export async function refreshDashboardData() {
  return fetchJson<{ status: "refreshed" }>("/api/refresh", { method: "POST" });
}

export async function applyCleanup(kind: "project" | "session", payload: CleanupApplyPayload) {
  return fetchJson<CleanupResult>(`/api/cleanup/${kind}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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
