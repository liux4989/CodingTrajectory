/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Measurement = "complete" | "partial" | "none";
export type Retention = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable = ("complete" | "preview" | "facts_only" | "none") | null;
export type SearchedResources = number | null;
export type Trimmed = boolean;
export type CompletedAt = string | null;
export type Hierarchy = "complete" | "partial";
export type Lifecycle = "complete" | "partial";
export type Measurement1 = "complete" | "partial" | "none";
export type Retention1 = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable1 = "complete" | "preview" | "facts_only" | "none";
export type Concept = string | null;
export type ExitCode = number | null;
export type Operation = string | null;
export type Path = string | null;
export type ResolutionKey = string | null;
export type Target = string | null;
export type TargetKind = ("file" | "search" | "command" | "web" | "coordination" | "tool") | null;
export type TargetSessionId = string | null;
export type ToolName = string | null;
export type VerificationKind = string | null;
export type EventIds = string[];
export type ItemId = string;
export type Kind = "agent_message" | "tool_call" | "command_execution" | "file_change" | "reasoning" | "plan";
export type InputChars = number;
export type InputTokens = number;
export type OutputChars = number;
export type OutputTokens = number;
export type TextChars = number;
export type TextTokens = number;
export type Operation1 = string | null;
export type Operations = string[] | null;
export type DurationMs = number | null;
export type ExitCode1 = number | null;
export type Facts = {
  [k: string]: boolean | number | string;
} | null;
export type Lifecycle1 = "completed" | "failed" | "interrupted" | "unknown";
export type OriginalTokens = number | null;
export type Outcome = string | null;
export type OutputChars1 = number;
export type OutputTokens1 = number | null;
export type Preview = string | null;
export type Processor = string;
export type ProcessorVersion = number;
export type Provider = string | null;
export type Retention2 = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable2 = "complete" | "preview" | "facts_only" | "none";
export type SourceEventIds = string[];
export type TokenMethod = "provider_reported" | "tokenizer_estimate" | "not_measured";
export type Tokenizer = string | null;
export type Truncated = boolean;
export type Preview1 = string | null;
export type Confidence = "high" | "medium" | "low";
export type Method = string;
export type Source = string;
export type SessionId = string;
export type SourceOrderKey = string;
export type SourceSequence = number;
export type StartedAt = string;
export type Status = string | null;
export type TurnId = string;
export type Type = string | null;
export type Items = CanonicalItemRecord[];
export type NextCursor = string | null;
export type Returned = number;
export type RootSessionId = string | null;
export type Total = number;
export type UnresolvedIds = string[];

export interface SessionItemsResponse {
  coverage?: ProjectionCoverage | null;
  items?: Items;
  next_cursor?: NextCursor;
  returned: Returned;
  root_session_id?: RootSessionId;
  total: Total;
  unresolved_ids?: UnresolvedIds;
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
export interface CanonicalItemRecord {
  completed_at?: CompletedAt;
  coverage: CanonicalResourceCoverage;
  detail?: CanonicalItemDetail | null;
  event_ids?: EventIds;
  item_id: ItemId;
  kind: Kind;
  measurements?: ItemContentMeasurements | null;
  operation?: Operation1;
  operations?: Operations;
  output_evidence?: ToolOutputEvidence | null;
  preview?: Preview1;
  provenance: CanonicalProvenance;
  session_id: SessionId;
  source_order_key: SourceOrderKey;
  source_sequence: SourceSequence;
  started_at: StartedAt;
  status?: Status;
  turn_id: TurnId;
  type?: Type;
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
/**
 * Bounded typed item detail; never a raw body.
 */
export interface CanonicalItemDetail {
  concept?: Concept;
  exit_code?: ExitCode;
  operation?: Operation;
  path?: Path;
  resolution_key?: ResolutionKey;
  target?: Target;
  target_kind?: TargetKind;
  target_session_id?: TargetSessionId;
  tool_name?: ToolName;
  verification_kind?: VerificationKind;
  [k: string]: unknown;
}
export interface ItemContentMeasurements {
  input_chars?: InputChars;
  input_tokens?: InputTokens;
  output_chars?: OutputChars;
  output_tokens?: OutputTokens;
  text_chars?: TextChars;
  text_tokens?: TextTokens;
  [k: string]: unknown;
}
/**
 * Public mirror of the retained per-item output evidence fact.
 */
export interface ToolOutputEvidence {
  duration_ms?: DurationMs;
  exit_code?: ExitCode1;
  facts?: Facts;
  lifecycle: Lifecycle1;
  original_tokens?: OriginalTokens;
  outcome?: Outcome;
  output_chars?: OutputChars1;
  output_tokens?: OutputTokens1;
  preview?: Preview;
  processor: Processor;
  processor_version: ProcessorVersion;
  provider?: Provider;
  retention: Retention2;
  searchable: Searchable2;
  source_event_ids?: SourceEventIds;
  token_method: TokenMethod;
  tokenizer?: Tokenizer;
  truncated?: Truncated;
  [k: string]: unknown;
}
export interface CanonicalProvenance {
  confidence: Confidence;
  method: Method;
  source: Source;
  [k: string]: unknown;
}
