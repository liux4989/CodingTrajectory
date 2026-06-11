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
      total_tokens: number;
      cost_usd: number;
      known_cost_count: number;
      missing_cost_count: number;
    };
    top_projects: Array<{
      project: string;
      count: number;
      vendors: Record<string, number>;
      execution_seconds: number;
      total_tokens: number;
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
      total_tokens: number;
      cost_usd: number;
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
  categories: ContextCategory[];
  provider_usage_buckets: ContextCategory[];
  events: ContextEvent[];
  warnings: string[];
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

export async function fetchOverview() {
  return fetchJson<OverviewPayload>("/api/overview");
}

export async function fetchProjects() {
  return fetchJson<{ items: ProjectItem[] }>("/api/projects");
}

export async function fetchSessions() {
  return fetchJson<{ items: SessionItem[] }>("/api/sessions");
}

export async function fetchContextWindow(sessionId: string) {
  const params = new URLSearchParams({ session_id: sessionId });
  return fetchJson<ContextWindowPayload>(`/api/sessions/context-window?${params}`);
}

export async function fetchCleanupPreview(kind: "project" | "session") {
  return fetchJson<CleanupPreview>(`/api/cleanup/${kind}/preview`);
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
