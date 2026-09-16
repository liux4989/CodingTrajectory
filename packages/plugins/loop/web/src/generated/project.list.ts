/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Path = string | null;
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
  path?: Path;
  sessions?: Sessions;
  vendors?: Vendors;
  [k: string]: unknown;
}
