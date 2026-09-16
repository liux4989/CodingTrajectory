/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type GraphId = string | null;
export type LineageRootSessionId = string | null;
export type Modified = string | null;
export type Preview = string | null;
export type Project = string | null;
export type RootSessionId = string;
export type Runtime = {
  [k: string]: unknown;
} | null;
export type SessionIds = string[];
export type Title = string | null;
export type Usage = {
  [k: string]: unknown;
} | null;
export type Vendors = string[];
export type Warnings = string[] | null;
export type Items = SessionGraphSummary[];

export interface ProjectSessionsResponse {
  items: Items;
  [k: string]: unknown;
}
export interface SessionGraphSummary {
  graph_id?: GraphId;
  lineage_root_session_id?: LineageRootSessionId;
  modified?: Modified;
  preview?: Preview;
  project?: Project;
  root_session_id: RootSessionId;
  runtime?: Runtime;
  session_ids?: SessionIds;
  title?: Title;
  usage?: Usage;
  vendors?: Vendors;
  warnings?: Warnings;
  [k: string]: unknown;
}
