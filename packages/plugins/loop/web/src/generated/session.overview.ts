/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type RootSessionId = string;
export type Sessions = {
  [k: string]: unknown;
}[];

export interface SessionOverviewResponse {
  root_session_id: RootSessionId;
  sessions: Sessions;
  [k: string]: unknown;
}
