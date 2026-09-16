/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type EstimatedCost = {
  [k: string]: unknown;
} | null;
export type RequestCount = number;
export type Requests = {
  [k: string]: unknown;
}[];
export type RootSessionId = string;
export type Warnings = string[];

export interface SessionRequestUsageResponse {
  estimated_cost?: EstimatedCost;
  request_count?: RequestCount;
  requests?: Requests;
  root_session_id: RootSessionId;
  usage?: Usage;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface Usage {
  [k: string]: unknown;
}
