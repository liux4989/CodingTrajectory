/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type WatchId = string;
export type StrategyId = string;
export type StrategyVersion = string;
export type Name = string;
export type ProjectName = string | null;
export type SessionId = string | null;
export type Measure =
  | "processed_tokens"
  | "prompt_completion_tokens"
  | "reported_total_tokens"
  | "prompt_tokens"
  | "uncached_prompt_tokens"
  | "cached_prompt_tokens"
  | "cache_write_tokens"
  | "completion_tokens"
  | "reasoning_tokens";
export type ThresholdTokens = number;
export type Severity = "info" | "warning" | "critical";
export type EmitFindings = boolean;
export type ConfigRevision = number;
export type Enabled = boolean;
export type CreatedAt = string;
export type UpdatedAt = string;
export type Revision = number;
export type ChangedAt = string;
/**
 * @maxItems 50
 */
export type Revisions = WatchRevision[];
export type Cursor = string | null;
export type Watermark = string | null;
export type LastRunAt = string | null;
export type CaughtUp = boolean;

export interface Watch {
  watch_id: WatchId;
  strategy_id?: StrategyId;
  strategy_version?: StrategyVersion;
  name: Name;
  scope: WatchScope;
  config: TokenBudgetConfig;
  config_revision?: ConfigRevision;
  enabled?: Enabled;
  created_at: CreatedAt;
  updated_at: UpdatedAt;
  revisions?: Revisions;
  refresh?: RefreshState;
}
/**
 * Exactly one scope selector. Membership semantics stay Core-owned.
 */
export interface WatchScope {
  project_name?: ProjectName;
  session_id?: SessionId;
}
/**
 * Human-approved watch configuration for the turn token-budget strategy.
 */
export interface TokenBudgetConfig {
  measure: Measure;
  threshold_tokens: ThresholdTokens;
  severity?: Severity;
  emit_findings?: EmitFindings;
}
/**
 * One effective configuration revision; historical results stay pinned.
 */
export interface WatchRevision {
  revision: Revision;
  scope: WatchScope;
  config: TokenBudgetConfig;
  changed_at: ChangedAt;
}
/**
 * Core `living.sessions` continuation position owned by one watch.
 */
export interface RefreshState {
  cursor?: Cursor;
  watermark?: Watermark;
  last_run_at?: LastRunAt;
  caught_up?: CaughtUp;
}
