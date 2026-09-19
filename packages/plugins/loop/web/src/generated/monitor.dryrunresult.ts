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
export type EvaluationId = string;
export type IdempotencyKey = string;
export type WatchId1 = string;
export type StrategyId1 = string;
export type StrategyVersion1 = string;
export type ConfigRevision1 = number;
export type Trigger = "historical_dry_run" | "manual_refresh";
export type SessionId1 = string;
export type TurnId = string | null;
export type ItemId = string | null;
export type EventId = string | null;
export type ViewManifestSha256 = string | null;
export type State = "completed" | "unavailable" | "pending" | "errored";
export type Result = ("pass" | "breach") | null;
export type Measure1 = string;
export type Observed = number | null;
export type ThresholdTokens1 = number;
export type Comparator = "<=";
export type Outcome = "pass" | "breach" | "unavailable" | "pending" | "error";
export type Summary = string;
export type Reason = string | null;
export type Type = "deterministic";
export type Rule = string;
export type RuleVersion = string;
export type EvidenceFingerprint = string;
export type SupersededBy = string | null;
export type FindingId = string | null;
export type StartedAt = string;
export type CompletedAt = string;
export type Evaluations = Evaluation[];
export type SessionsExamined = number;
export type TurnsEvaluated = number;
export type Passed = number;
export type Breached = number;
export type Unavailable = number;
export type Pending = number;
export type Errored = number;
export type SkippedUnchanged = number;
export type FindingId1 = string;
export type DedupeKey = string;
export type WatchId2 = string;
export type EvaluationId1 = string;
export type StrategyId2 = string;
export type StrategyVersion2 = string;
export type ConfigRevision2 = number;
export type Severity1 = "info" | "warning" | "critical";
export type Status = "open" | "acknowledged" | "resolved" | "dismissed";
export type Title = string;
export type CreatedAt1 = string;
export type UpdatedAt1 = string;
export type Status1 = "open" | "acknowledged" | "resolved" | "dismissed";
export type At = string;
/**
 * @maxItems 100
 */
export type StatusHistory = FindingStatusEvent[];
export type ProspectiveFindings = Finding[];
export type ScopeSessionsTotal = number;
export type Truncated = boolean;
export type Notes = string[];

/**
 * Labeled historical preview. Never persists findings.
 */
export interface DryRunResult {
  watch: Watch;
  evaluations: Evaluations;
  summary: EvaluationSummary;
  prospective_findings: ProspectiveFindings;
  scope_sessions_total: ScopeSessionsTotal;
  truncated: Truncated;
  notes?: Notes;
}
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
/**
 * Every attempted strategy execution over one canonical turn.
 */
export interface Evaluation {
  evaluation_id: EvaluationId;
  idempotency_key: IdempotencyKey;
  watch_id: WatchId1;
  strategy_id: StrategyId1;
  strategy_version: StrategyVersion1;
  config_revision: ConfigRevision1;
  trigger: Trigger;
  reference: CanonicalReference;
  state: State;
  result: Result;
  condition: ConditionExplanation;
  coverage?: Coverage;
  reason?: Reason;
  evaluator: EvaluatorSpec;
  evidence_fingerprint: EvidenceFingerprint;
  superseded_by?: SupersededBy;
  finding_id?: FindingId;
  started_at: StartedAt;
  completed_at: CompletedAt;
}
export interface CanonicalReference {
  session_id: SessionId1;
  turn_id?: TurnId;
  item_id?: ItemId;
  event_id?: EventId;
  view_manifest_sha256?: ViewManifestSha256;
}
/**
 * Explicit condition record: measure, observed value, threshold, outcome.
 */
export interface ConditionExplanation {
  measure: Measure1;
  observed: Observed;
  threshold_tokens: ThresholdTokens1;
  comparator?: Comparator;
  outcome: Outcome;
  summary: Summary;
}
export interface Coverage {
  [k: string]: unknown;
}
/**
 * Evaluator provenance recorded on every evaluation.
 */
export interface EvaluatorSpec {
  type?: Type;
  rule?: Rule;
  rule_version?: RuleVersion;
}
export interface EvaluationSummary {
  sessions_examined?: SessionsExamined;
  turns_evaluated?: TurnsEvaluated;
  passed?: Passed;
  breached?: Breached;
  unavailable?: Unavailable;
  pending?: Pending;
  errored?: Errored;
  skipped_unchanged?: SkippedUnchanged;
}
/**
 * Actionable work item emitted by a breach evaluation.
 */
export interface Finding {
  finding_id: FindingId1;
  dedupe_key: DedupeKey;
  watch_id: WatchId2;
  evaluation_id: EvaluationId1;
  strategy_id: StrategyId2;
  strategy_version: StrategyVersion2;
  config_revision: ConfigRevision2;
  severity: Severity1;
  status?: Status;
  title: Title;
  condition: ConditionExplanation;
  reference: CanonicalReference;
  created_at: CreatedAt1;
  updated_at: UpdatedAt1;
  status_history?: StatusHistory;
}
export interface FindingStatusEvent {
  status: Status1;
  at: At;
}
