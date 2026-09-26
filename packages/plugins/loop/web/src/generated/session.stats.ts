/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type BilledTokenUsage = {
  [k: string]: unknown;
} | null;
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
  context_window?: ContextWindow;
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
