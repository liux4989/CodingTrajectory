/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type CacheAttribution = {
  [k: string]: unknown;
} | null;
export type Compaction = {
  [k: string]: unknown;
} | null;
export type EffortChanges = {
  [k: string]: unknown;
} | null;
export type EstimatedCost = {
  [k: string]: unknown;
} | null;
export type Models = {
  [k: string]: unknown;
}[];
export type Scope = string | null;
export type SelectedTurnId = string | null;
export type SessionId = string;
export type Sessions =
  | {
      [k: string]: unknown;
    }[]
  | null;
export type Turns =
  | {
      [k: string]: unknown;
    }[]
  | null;
export type Warnings = string[];

export interface SessionUsageResponse {
  cache_attribution?: CacheAttribution;
  compaction?: Compaction;
  effort_changes?: EffortChanges;
  estimated_cost?: EstimatedCost;
  models?: Models;
  runtime?: Runtime;
  scope?: Scope;
  selected_turn_id?: SelectedTurnId;
  session_id: SessionId;
  sessions?: Sessions;
  total_usage: TotalUsage;
  turns?: Turns;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface Runtime {
  [k: string]: unknown;
}
export interface TotalUsage {
  [k: string]: unknown;
}
