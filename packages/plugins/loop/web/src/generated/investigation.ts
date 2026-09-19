/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Id = string;
export type Title = string;
export type SessionId = string;
export type TurnId = string | null;
export type ItemId = string | null;
export type EventId = string | null;
export type ViewManifestSha256 = string | null;
export type Source = "host_local";
export type Revision = "latest";

export interface Investigation {
  id: Id;
  title: Title;
  reference: CanonicalReference;
  source?: Source;
  revision?: Revision;
}
export interface CanonicalReference {
  session_id: SessionId;
  turn_id?: TurnId;
  item_id?: ItemId;
  event_id?: EventId;
  view_manifest_sha256?: ViewManifestSha256;
}
