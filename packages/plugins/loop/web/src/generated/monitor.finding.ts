/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type FindingId = string;
export type DedupeKey = string;
export type WatchId = string;
export type EvaluationId = string;
export type StrategyId = string;
export type StrategyVersion = string;
export type ConfigRevision = number;
export type Severity = "info" | "warning" | "critical";
export type Status = "open" | "acknowledged" | "resolved" | "dismissed";
export type Title = string;
export type Measure = string;
export type Observed = number | null;
export type ThresholdTokens = number;
export type Comparator = "<=";
export type Outcome = "pass" | "breach" | "unavailable" | "pending" | "error";
export type Summary = string;
export type SessionId = string;
export type TurnId = string | null;
export type ItemId = string | null;
export type EventId = string | null;
export type CreatedAt = string;
export type UpdatedAt = string;
export type Status1 = "open" | "acknowledged" | "resolved" | "dismissed";
export type At = string;
/**
 * @maxItems 100
 */
export type StatusHistory = FindingStatusEvent[];

/**
 * Actionable work item emitted by a breach evaluation.
 */
export interface Finding {
  finding_id: FindingId;
  dedupe_key: DedupeKey;
  watch_id: WatchId;
  evaluation_id: EvaluationId;
  strategy_id: StrategyId;
  strategy_version: StrategyVersion;
  config_revision: ConfigRevision;
  severity: Severity;
  status?: Status;
  title: Title;
  condition: ConditionExplanation;
  reference: CanonicalReference;
  created_at: CreatedAt;
  updated_at: UpdatedAt;
  status_history?: StatusHistory;
}
/**
 * Explicit condition record: measure, observed value, threshold, outcome.
 */
export interface ConditionExplanation {
  measure: Measure;
  observed: Observed;
  threshold_tokens: ThresholdTokens;
  comparator?: Comparator;
  outcome: Outcome;
  summary: Summary;
}
export interface CanonicalReference {
  session_id: SessionId;
  turn_id?: TurnId;
  item_id?: ItemId;
  event_id?: EventId;
}
export interface FindingStatusEvent {
  status: Status1;
  at: At;
}
