/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type EvaluationId = string;
export type IdempotencyKey = string;
export type WatchId = string;
export type StrategyId = string;
export type StrategyVersion = string;
export type ConfigRevision = number;
export type Trigger = "historical_dry_run" | "manual_refresh";
export type SessionId = string;
export type TurnId = string | null;
export type ItemId = string | null;
export type EventId = string | null;
export type State = "completed" | "unavailable" | "pending" | "errored";
export type Result = ("pass" | "breach") | null;
export type Measure = string;
export type Observed = number | null;
export type ThresholdTokens = number;
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

/**
 * Every attempted strategy execution over one canonical turn.
 */
export interface Evaluation {
  evaluation_id: EvaluationId;
  idempotency_key: IdempotencyKey;
  watch_id: WatchId;
  strategy_id: StrategyId;
  strategy_version: StrategyVersion;
  config_revision: ConfigRevision;
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
  session_id: SessionId;
  turn_id?: TurnId;
  item_id?: ItemId;
  event_id?: EventId;
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
