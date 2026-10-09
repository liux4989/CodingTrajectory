/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type GraphId = string | null;
export type LineageRootSessionId = string | null;
export type Modified = string | null;
export type Preview = string | null;
export type Project = string | null;
export type ProjectId = string | null;
export type RootSessionId = string;
export type SessionIds = string[];
export type Title = string | null;
export type Vendors = string[];
export type Items = SessionGraphSummary[];
export type NextCursor = string | null;
export type Returned = number;
export type Total = number;

export interface ProjectSessionsResponse {
  items: Items;
  next_cursor?: NextCursor;
  returned: Returned;
  total: Total;
  [k: string]: unknown;
}
export interface SessionGraphSummary {
  graph_id?: GraphId;
  lineage_root_session_id?: LineageRootSessionId;
  modified?: Modified;
  preview?: Preview;
  project?: Project;
  project_id?: ProjectId;
  root_session_id: RootSessionId;
  session_ids?: SessionIds;
  title?: Title;
  vendors?: Vendors;
  [k: string]: unknown;
}
