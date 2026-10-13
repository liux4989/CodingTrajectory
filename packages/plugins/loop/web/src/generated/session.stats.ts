/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type BilledTokenUsage = {
  [k: string]: unknown;
} | null;
export type Compaction = {
  [k: string]: unknown;
} | null;
export type CostSummary = {
  [k: string]: unknown;
} | null;
export type Complete = number;
export type Partial = number;
export type Unknown = number;
export type ObservedCalls = number;
export type Scripts = number;
export type SourceOnlyCalls = number;
export type ProviderUsageBuckets = {
  [k: string]: unknown;
}[];
export type RootSessionId = string | null;
export type Scope = string | null;
export type Sessions =
  | {
      [k: string]: unknown;
    }[]
  | null;
export type Vendor = string | null;
export type Warnings = string[];

export interface SessionStatsResponse {
  billed_token_usage?: BilledTokenUsage;
  compaction?: Compaction;
  context_window?: ContextWindow;
  cost_summary?: CostSummary;
  execution: ExecutionCounts;
  messages?: Messages;
  model?: Model;
  provider_usage_buckets?: ProviderUsageBuckets;
  root_session_id?: RootSessionId;
  runtime?: Runtime;
  scope?: Scope;
  sessions?: Sessions;
  usage?: Usage;
  vendor?: Vendor;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface ContextWindow {
  [k: string]: unknown;
}
export interface ExecutionCounts {
  coverage: ScriptInventoryCoverage;
  observed_calls: ObservedCalls;
  scripts: Scripts;
  source_only_calls: SourceOnlyCalls;
}
export interface ScriptInventoryCoverage {
  complete: Complete;
  partial: Partial;
  unknown: Unknown;
}
export interface Messages {
  [k: string]: unknown;
}
export interface Model {
  [k: string]: unknown;
}
export interface Runtime {
  [k: string]: unknown;
}
export interface Usage {
  [k: string]: unknown;
}
