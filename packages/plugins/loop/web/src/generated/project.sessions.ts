/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type GraphId = string | null;
export type LineageRootSessionId = string | null;
export type Modified = string | null;
export type Preview = string | null;
export type Project = string | null;
export type ProjectId = string | null;
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
export type ViewManifestSha256 = string | null;
export type Warnings = string[] | null;
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
  runtime?: Runtime;
  session_ids?: SessionIds;
  title?: Title;
  usage?: Usage;
  vendors?: Vendors;
  view_manifest_sha256?: ViewManifestSha256;
  warnings?: Warnings;
  [k: string]: unknown;
}
