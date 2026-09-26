/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type EstimatedCost = {
  [k: string]: unknown;
} | null;
export type NextCursor = string | null;
export type RequestCount = number;
export type Requests = {
  [k: string]: unknown;
}[];
export type Returned = number;
export type RootSessionId = string;
export type Total = number;
export type Warnings = string[];

export interface SessionRequestUsageResponse {
  estimated_cost?: EstimatedCost;
  next_cursor?: NextCursor;
  request_count?: RequestCount;
  requests?: Requests;
  returned: Returned;
  root_session_id: RootSessionId;
  total: Total;
  usage?: Usage;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface Usage {
  [k: string]: unknown;
}
