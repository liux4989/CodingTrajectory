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

export interface ProjectListResponse {
  items: Items;
  [k: string]: unknown;
}
export interface Items {
  [k: string]: ProjectSummary;
}
export interface ProjectSummary {
  display_name: DisplayName;
  path?: Path;
  project_id: ProjectId;
  sessions?: Sessions;
  vendors?: Vendors;
  [k: string]: unknown;
}
