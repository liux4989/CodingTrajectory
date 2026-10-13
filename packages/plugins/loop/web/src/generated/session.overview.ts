/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Truncated = boolean;
export type SessionId = string;
export type Title = string | null;
/**
 * @minItems 1
 */
export type ItemIds = [string, ...string[]];
export type Text = string;
export type Truncated1 = boolean;
export type Type = "assistant";
/**
 * @minItems 1
 */
export type ItemIds1 = [string, ...string[]];
export type Outcome = ("succeeded" | "failed") | null;
export type Status = string | null;
export type Text1 = string;
export type Tool = string;
export type Truncated2 = boolean;
export type Type1 = "activity";
export type Items = (NarrativeAssistant | NarrativeActivity)[];
export type EventId = string | null;
export type Text2 = string;
export type Truncated3 = boolean;
export type Sequence = number;
export type StartedAt = string | null;
export type Status1 = string;
export type TurnId = string;
export type Turns = NarrativeTurn[];

export interface SessionOverviewResponse {
  coverage: NarrativeCoverage;
  session_id: SessionId;
  title: Title;
  turns: Turns;
  [k: string]: unknown;
}
export interface NarrativeCoverage {
  truncated: Truncated;
}
export interface NarrativeTurn {
  items: Items;
  request: NarrativeRequest | null;
  sequence: Sequence;
  started_at: StartedAt;
  status: Status1;
  turn_id: TurnId;
}
export interface NarrativeAssistant {
  item_ids: ItemIds;
  text: Text;
  truncated: Truncated1;
  type?: Type;
}
export interface NarrativeActivity {
  item_ids: ItemIds1;
  outcome: Outcome;
  status: Status;
  text: Text1;
  tool: Tool;
  truncated: Truncated2;
  type?: Type1;
}
export interface NarrativeRequest {
  event_id: EventId;
  text: Text2;
  truncated: Truncated3;
}
