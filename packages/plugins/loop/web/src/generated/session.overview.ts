/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type SelectionTrimmed = boolean;
export type SessionId = string;
export type Title = string | null;
/**
 * @minItems 1
 */
export type ItemIds = [string, ...string[]];
export type Kind = "assistant";
export type Preview = string;
export type Truncated = boolean;
export type ActivityType = string;
/**
 * @minItems 1
 */
export type ItemIds1 = [string, ...string[]];
export type Kind1 = "activity";
export type Outcome = ("succeeded" | "failed") | null;
export type Preview1 = string;
export type Status = string | null;
export type Truncated1 = boolean;
export type Entries = (NarrativeAssistant | NarrativeActivity)[];
export type SourceSequence = number;
export type StartedAt = string | null;
export type Status1 = string;
export type TurnId = string;
export type EventId = string | null;
export type Preview2 = string;
export type Truncated2 = boolean;
export type Turns = NarrativeTurn[];

export interface SessionOverviewResponse {
  coverage: NarrativeCoverage;
  session_id: SessionId;
  title: Title;
  turns: Turns;
  [k: string]: unknown;
}
export interface NarrativeCoverage {
  selection_trimmed: SelectionTrimmed;
}
export interface NarrativeTurn {
  entries: Entries;
  source_sequence: SourceSequence;
  started_at: StartedAt;
  status: Status1;
  turn_id: TurnId;
  user_request: NarrativeRequest | null;
}
export interface NarrativeAssistant {
  item_ids: ItemIds;
  kind?: Kind;
  preview: Preview;
  truncated: Truncated;
}
export interface NarrativeActivity {
  activity_type: ActivityType;
  item_ids: ItemIds1;
  kind?: Kind1;
  outcome: Outcome;
  preview: Preview1;
  status: Status;
  truncated: Truncated1;
}
export interface NarrativeRequest {
  event_id: EventId;
  preview: Preview2;
  truncated: Truncated2;
}
