/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type DisplayName = string;
export type Path = string | null;
export type ProjectId = string;
export type Sessions =
  | {
      [k: string]: unknown;
    }[]
  | null;
export type Vendors = string[];
export type Items = ProjectSummary[];
export type NextCursor = string | null;
export type Returned = number;
export type Total = number;

export interface ProjectListResponse {
  items: Items;
  next_cursor?: NextCursor;
  returned: Returned;
  total: Total;
  [k: string]: unknown;
}
export interface ProjectSummary {
  display_name: DisplayName;
  path?: Path;
  project_id: ProjectId;
  sessions?: Sessions;
  vendors?: Vendors;
  [k: string]: unknown;
}
