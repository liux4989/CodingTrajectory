/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Measurement = "complete" | "partial" | "none";
export type Retention = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable = ("complete" | "preview" | "facts_only" | "none") | null;
export type SearchedResources = number | null;
export type Trimmed = boolean;
export type Hierarchy = "complete" | "partial";
export type Lifecycle = "complete" | "partial";
export type Measurement1 = "complete" | "partial" | "none";
export type Retention1 = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable1 = "complete" | "preview" | "facts_only" | "none";
export type EventId = string;
export type ItemId = string | null;
export type OutputEvidenceId = string | null;
export type Confidence = "high" | "medium" | "low";
export type Method = string;
export type Source = string;
export type SessionId = string;
export type SourceOrderKey = string;
export type SourceSequence = number;
export type Status = string | null;
export type Timestamp = string;
export type TurnId = string | null;
export type Type = string;
export type CacheCreationInputTokens = number;
export type CachedInputTokens = number;
export type ContextWindowTokens = number | null;
export type CostUsd = number | null;
export type InputTokens = number;
export type Model = string | null;
export type OutputTokens = number;
export type Provider = string | null;
export type ReasoningOutputTokens = number;
export type Source1 = string | null;
export type TotalTokens = number;
export type UncachedInputTokens = number | null;
export type UsedInputTokens = number;
export type Events = CanonicalEventRecord[];
export type NextCursor = string | null;
export type RootSessionId = string | null;

export interface SessionEventsResponse {
  coverage?: ProjectionCoverage | null;
  events?: Events;
  next_cursor?: NextCursor;
  root_session_id?: RootSessionId;
  [k: string]: unknown;
}
/**
 * Honest bounded-evidence coverage for summary/search/overview results.
 *
 * ``retention`` distinguishes how much raw content the authority retained:
 * ``not_applicable`` (no content concept), ``not_retained`` (measured but
 * discarded), ``preview`` (bounded redacted preview), ``complete`` (never for
 * sanitized content). ``searchable`` declares searchable completeness.
 */
export interface ProjectionCoverage {
  measurement: Measurement;
  retention: Retention;
  searchable?: Searchable;
  searched_resources?: SearchedResources;
  trimmed?: Trimmed;
  [k: string]: unknown;
}
/**
 * Minimal normalized event envelope; never a raw payload.
 *
 * Output content is referenced, not embedded: ``item_id`` resolves the owning
 * item, whose ``output_evidence`` carries the retained processed evidence.
 */
export interface CanonicalEventRecord {
  coverage: CanonicalResourceCoverage;
  event_id: EventId;
  item_id?: ItemId;
  output_evidence_id?: OutputEvidenceId;
  provenance: CanonicalProvenance;
  session_id: SessionId;
  source_order_key: SourceOrderKey;
  source_sequence: SourceSequence;
  status?: Status;
  timestamp: Timestamp;
  turn_id?: TurnId;
  type: Type;
  usage?: CanonicalEventUsage | null;
  [k: string]: unknown;
}
/**
 * Per-resource bounded-evidence coverage for items and events.
 */
export interface CanonicalResourceCoverage {
  hierarchy: Hierarchy;
  lifecycle: Lifecycle;
  measurement: Measurement1;
  retention: Retention1;
  searchable: Searchable1;
  [k: string]: unknown;
}
export interface CanonicalProvenance {
  confidence: Confidence;
  method: Method;
  source: Source;
  [k: string]: unknown;
}
/**
 * Native numeric usage measurements for one usage-observation event.
 */
export interface CanonicalEventUsage {
  cache_creation_input_tokens?: CacheCreationInputTokens;
  cached_input_tokens?: CachedInputTokens;
  context_window_tokens?: ContextWindowTokens;
  cost_usd?: CostUsd;
  input_tokens?: InputTokens;
  model?: Model;
  output_tokens?: OutputTokens;
  provider?: Provider;
  reasoning_output_tokens?: ReasoningOutputTokens;
  source?: Source1;
  total_tokens?: TotalTokens;
  uncached_input_tokens?: UncachedInputTokens;
  used_input_tokens?: UsedInputTokens;
  [k: string]: unknown;
}
