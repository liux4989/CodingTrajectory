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
export type Calls = NarrativeToolCall[] | null;
export type ItemId = string | null;
export type Observation = "observed" | "source_only";
export type Arguments = {
  [k: string]: unknown;
} | null;
export type Command = string | string[] | null;
export type CommandRef = {
  [k: string]: unknown;
} | null;
export type LaunchRef = {
  [k: string]: unknown;
} | null;
export type OutputRef = {
  [k: string]: unknown;
} | null;
export type PollOutputRef = {
  [k: string]: unknown;
} | null;
export type ProcessId = string;
export type Resolution = "resolved" | "unresolved" | "ambiguous";
export type Status = ("succeeded" | "failed" | "unfinished") | null;
export type SummaryBasis = ("process_command" | "process_id") | null;
export type Text1 = string;
export type Tool = string;
export type Truncated2 = boolean;
/**
 * @minItems 1
 */
export type ItemIds1 = [string, ...string[]];
export type Outcome = ("succeeded" | "failed") | null;
export type Status1 = string | null;
export type Text2 = string;
export type Tool1 = string;
export type Truncated3 = boolean;
export type Type1 = "activity";
export type Items = (NarrativeAssistant | NarrativeActivity)[];
export type EventId = string | null;
export type Text3 = string;
export type Truncated4 = boolean;
export type Sequence = number;
export type StartedAt = string | null;
export type Status2 = string;
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
  status: Status2;
  turn_id: TurnId;
}
export interface NarrativeAssistant {
  item_ids: ItemIds;
  text: Text;
  truncated: Truncated1;
  type?: Type;
}
export interface NarrativeActivity {
  calls?: Calls;
  item_ids: ItemIds1;
  outcome: Outcome;
  status: Status1;
  text: Text2;
  tool: Tool1;
  truncated: Truncated3;
  type?: Type1;
}
export interface NarrativeToolCall {
  item_id: ItemId;
  observation: Observation;
  process?: ProcessTrace | null;
  status: Status;
  summary_basis?: SummaryBasis;
  text: Text1;
  tool: Tool;
  truncated: Truncated2;
}
export interface ProcessTrace {
  arguments?: Arguments;
  command?: Command;
  command_ref?: CommandRef;
  launch_ref?: LaunchRef;
  output_ref?: OutputRef;
  poll_arguments: PollArguments;
  poll_output_ref?: PollOutputRef;
  poll_ref: PollRef;
  process_id: ProcessId;
  resolution: Resolution;
}
export interface PollArguments {
  [k: string]: unknown;
}
export interface PollRef {
  [k: string]: unknown;
}
export interface NarrativeRequest {
  event_id: EventId;
  text: Text3;
  truncated: Truncated4;
}
