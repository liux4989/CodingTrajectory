/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Operations = string[];
export type Path = string;
export type EventIds = string[];
export type ItemId = string | null;
export type SessionId = string;
export type TurnId = string | null;
export type Changes = SummaryChange[];
export type Measurement = "complete" | "partial" | "none";
export type Retention = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable = ("complete" | "preview" | "facts_only" | "none") | null;
export type SearchedResources = number | null;
export type Trimmed = boolean;
export type Text = string;
export type Decisions = SummaryClaim[];
export type LatestTurnStatus = string | null;
export type NextActions = SummaryClaim[];
export type Name = string;
export type Strategy = string;
export type Version = number;
export type Kind = string;
export type Label = string;
export type Status = string | null;
export type RecentActivity = SummaryActivity[];
export type SelectedTurnId = string | null;
export type SessionId1 = string;
export type Total = number;
export type Truncated = boolean;
export type Label1 = string;
export type Status1 = string;
export type Unresolved = SummaryEvidence[];
export type Verification = SummaryEvidence[];
export type Warnings = string[];

export interface SessionSummaryResponse {
  changes?: Changes;
  coverage: ProjectionCoverage;
  decisions?: Decisions;
  latest_turn_status?: LatestTurnStatus;
  next_actions?: NextActions;
  objective?: SummaryClaim | null;
  projection: ProjectionIdentity;
  recent_activity?: RecentActivity;
  selected_turn_id?: SelectedTurnId;
  session_id: SessionId1;
  truncation?: Truncation;
  unresolved?: Unresolved;
  verification?: Verification;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface SummaryChange {
  operations?: Operations;
  path: Path;
  references: EvidenceReferences;
  [k: string]: unknown;
}
export interface EvidenceReferences {
  event_ids?: EventIds;
  item_id?: ItemId;
  session_id: SessionId;
  turn_id?: TurnId;
  [k: string]: unknown;
}
/**
 * Honest bounded-evidence coverage for summary/search/overview results.
 *
 * ``retention`` distinguishes how much raw content the authority retained:
 * ``not_applicable`` (no content concept), ``not_retained`` (measured but
 * discarded), ``preview`` (bounded redacted preview), ``complete`` (never for
 * sanitized content). ``searchable`` declares searchable completeness.
 */
export interface ProjectionCoverage {
  measurement: Measurement;
  retention: Retention;
  searchable?: Searchable;
  searched_resources?: SearchedResources;
  trimmed?: Trimmed;
  [k: string]: unknown;
}
export interface SummaryClaim {
  references: EvidenceReferences;
  text: Text;
  [k: string]: unknown;
}
export interface ProjectionIdentity {
  name: Name;
  strategy: Strategy;
  version: Version;
  [k: string]: unknown;
}
export interface SummaryActivity {
  kind: Kind;
  label: Label;
  references: EvidenceReferences;
  status?: Status;
  [k: string]: unknown;
}
export interface Truncation {
  [k: string]: TruncationStatus;
}
export interface TruncationStatus {
  total: Total;
  truncated: Truncated;
  [k: string]: unknown;
}
export interface SummaryEvidence {
  label: Label1;
  references: EvidenceReferences;
  status: Status1;
  [k: string]: unknown;
}
