/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Operations = string[];
export type Path = string;
export type EventIds = string[];
export type ItemId = string | null;
export type ScriptId = string | null;
export type SessionId = string;
export type TurnId = string | null;
export type Changes = SummaryChange[];
export type Retention = "not_applicable" | "not_retained" | "preview" | "complete";
export type Searchable = ("complete" | "preview" | "facts_only" | "none") | null;
export type SearchedResources = number | null;
export type Trimmed = boolean;
export type Text = string;
export type Decisions = SummaryClaim[];
export type Attribution = "membership_does_not_allocate_content_or_usage";
export type Count = number;
export type ToolName = string;
export type ObservedCalls = ToolObservationCount[];
export type Scope = "returned_execution_units";
export type ScriptExecutions = number;
export type SourceOnlySites = ToolObservationCount[];
export type SessionId1 = string;
export type TurnId1 = string;
export type ItemId1 = string;
export type Kind = "item";
export type OpenSourceLink = string | null;
export type ItemId2 = string;
export type Language = string;
export type Link = string;
export type LocalOnly = boolean;
export type ProviderCallId = string | null;
export type RecordDigests = string[];
export type RecordIndices = number[];
export type SessionId2 = string;
export type SourceField = string;
export type SourceLog = string;
export type SourceLogSha256 = string;
export type SourceSha256 = string;
export type ToolName1 = string | null;
export type UnitId = string;
export type FollowingAssistantItemIds = string[];
export type NextItemId = string | null;
export type PrecedingAssistantItemIds = string[];
export type PreviousItemId = string | null;
export type SessionId3 = string;
export type TurnId2 = string;
export type UserRequestEventId = string | null;
export type ControlItemIds = string[];
export type InventoryCoverage = "complete" | "partial" | "unknown";
export type ItemId3 = string;
export type ItemIds = string[];
export type Kind1 = "tool_script";
export type LocalOnly1 = boolean;
export type ArgumentsCoverage = "recorded" | "not_recorded" | "truncated" | "source_reconstructed";
export type ArgumentsPreview = string | null;
export type ArgumentsRef = {
  [k: string]: unknown;
} | null;
export type CompletedAt = string | null;
export type DurationMs = number | null;
export type Failure = string | null;
export type ItemId4 = string | null;
export type Observation = "observed" | "source_only";
export type Outcome = ("succeeded" | "failed" | "unfinished") | null;
export type OwnershipBasis = string;
export type ProviderCallId1 = string | null;
export type ResultCoverage = "recorded" | "not_recorded";
export type ResultRef = {
  [k: string]: unknown;
} | null;
export type SourceToolName = string | null;
export type StartedAt = string | null;
export type ToolName2 = string;
export type ObservedExternalCalls = ExternalCall[];
export type OpenSourceLink1 = string;
export type EmittedOutputRef = {
  [k: string]: unknown;
} | null;
export type FailureOutputRef = {
  [k: string]: unknown;
} | null;
export type InvocationCompletedAt = string | null;
export type InvocationStatus = string | null;
export type Lifecycle = "completed" | "failed" | "yielded" | "interrupted" | "unknown";
export type LifecycleCoverage = "invocation_only" | "runtime_checkpoint";
export type OutputCoverage = "recorded" | "not_recorded";
export type OutputScope = "invocation" | "script";
export type RuntimeCompletedAt = string | null;
export type RuntimeExecutionId = string | null;
export type StartedAt1 = string;
export type SchemaVersion = "ct.tool_script.v1";
export type SessionId4 = string;
export type SourceAnalysisCoverage = "partial" | "syntax_errors" | "not_supported";
export type SourceOnlyCalls = ExternalCall[];
export type SourcePreview = string | null;
export type SourcePreviewTruncated = boolean;
export type ToolName3 = string | null;
export type TurnId3 = string;
export type UnitId1 = string;
export type Units = (DirectItemReference | ToolScriptExecution)[];
export type ExecutionUnits = TurnExecutionLayer[];
export type LatestTurnStatus = string | null;
export type NextActions = SummaryClaim[];
export type Name = string;
export type Strategy = string;
export type Version = number;
export type Kind2 = string;
export type Label = string;
export type Status = string | null;
export type RecentActivity = SummaryActivity[];
export type SelectedTurnId = string | null;
export type SessionId5 = string;
export type Total = number;
export type Truncated = boolean;
export type Label1 = string;
export type Status1 = string;
export type Unresolved = SummaryEvidence[];
export type Verification = SummaryEvidence[];
export type Warnings = string[];

export interface SessionSummaryResponse {
  changes?: Changes;
  coverage: ProjectionCoverage;
  decisions?: Decisions;
  execution_composition?: ExecutionComposition | null;
  execution_units?: ExecutionUnits;
  latest_turn_status?: LatestTurnStatus;
  next_actions?: NextActions;
  objective?: SummaryClaim | null;
  projection: ProjectionIdentity;
  recent_activity?: RecentActivity;
  selected_turn_id?: SelectedTurnId;
  session_id: SessionId5;
  truncation?: Truncation;
  unresolved?: Unresolved;
  verification?: Verification;
  warnings?: Warnings;
  [k: string]: unknown;
}
export interface SummaryChange {
  operations?: Operations;
  path: Path;
  references: EvidenceReferences;
  [k: string]: unknown;
}
export interface EvidenceReferences {
  event_ids?: EventIds;
  item_id?: ItemId;
  script_id?: ScriptId;
  session_id: SessionId;
  turn_id?: TurnId;
  [k: string]: unknown;
}
/**
 * Content availability and selection completeness for local projections.
 */
export interface ProjectionCoverage {
  retention: Retention;
  searchable?: Searchable;
  searched_resources?: SearchedResources;
  trimmed?: Trimmed;
  [k: string]: unknown;
}
export interface SummaryClaim {
  references: EvidenceReferences;
  text: Text;
  [k: string]: unknown;
}
export interface ExecutionComposition {
  attribution?: Attribution;
  observed_calls: ObservedCalls;
  scope?: Scope;
  script_executions: ScriptExecutions;
  script_inventory_coverage: ScriptInventoryCoverage;
  source_only_sites: SourceOnlySites;
}
export interface ToolObservationCount {
  count: Count;
  tool_name: ToolName;
}
export interface ScriptInventoryCoverage {
  [k: string]: number;
}
export interface TurnExecutionLayer {
  session_id: SessionId1;
  turn_id: TurnId1;
  units?: Units;
}
export interface DirectItemReference {
  item_id: ItemId1;
  kind?: Kind;
  open_source_link?: OpenSourceLink;
  source?: SourceReference | null;
  unit_id: UnitId;
}
export interface SourceReference {
  item_id: ItemId2;
  language: Language;
  link: Link;
  local_only?: LocalOnly;
  provider_call_id?: ProviderCallId;
  record_digests: RecordDigests;
  record_indices: RecordIndices;
  session_id: SessionId2;
  source_field: SourceField;
  source_log: SourceLog;
  source_log_sha256: SourceLogSha256;
  source_sha256: SourceSha256;
  tool_name?: ToolName1;
}
export interface ToolScriptExecution {
  context_refs: ContextReferences;
  control_item_ids?: ControlItemIds;
  inspection_log_ref: InspectionLogRef;
  inventory_coverage: InventoryCoverage;
  item_id: ItemId3;
  item_ids: ItemIds;
  kind?: Kind1;
  local_only?: LocalOnly1;
  observed_external_calls: ObservedExternalCalls;
  open_source_link: OpenSourceLink1;
  result: ExecutionResult;
  schema_version?: SchemaVersion;
  session_id: SessionId4;
  source: SourceReference;
  source_analysis_coverage?: SourceAnalysisCoverage;
  source_only_calls: SourceOnlyCalls;
  source_preview?: SourcePreview;
  source_preview_truncated?: SourcePreviewTruncated;
  tool_name?: ToolName3;
  turn_id: TurnId3;
  unit_id: UnitId1;
}
export interface ContextReferences {
  following_assistant_item_ids: FollowingAssistantItemIds;
  next_item_id?: NextItemId;
  preceding_assistant_item_ids: PrecedingAssistantItemIds;
  previous_item_id?: PreviousItemId;
  session_id: SessionId3;
  turn_id: TurnId2;
  user_request_event_id?: UserRequestEventId;
}
export interface InspectionLogRef {
  [k: string]: string;
}
export interface ExternalCall {
  arguments_coverage: ArgumentsCoverage;
  arguments_preview?: ArgumentsPreview;
  arguments_ref?: ArgumentsRef;
  completed_at?: CompletedAt;
  duration_ms?: DurationMs;
  evidence_ref: EvidenceRef;
  failure?: Failure;
  item_id?: ItemId4;
  observation: Observation;
  outcome?: Outcome;
  ownership_basis: OwnershipBasis;
  provider_call_id?: ProviderCallId1;
  result_coverage?: ResultCoverage;
  result_ref?: ResultRef;
  source_tool_name?: SourceToolName;
  started_at?: StartedAt;
  tool_name: ToolName2;
}
export interface EvidenceRef {
  [k: string]: unknown;
}
export interface ExecutionResult {
  emitted_output_ref?: EmittedOutputRef;
  failure_output_ref?: FailureOutputRef;
  invocation_completed_at?: InvocationCompletedAt;
  invocation_status?: InvocationStatus;
  lifecycle: Lifecycle;
  lifecycle_coverage: LifecycleCoverage;
  output_coverage: OutputCoverage;
  output_scope?: OutputScope;
  runtime_completed_at?: RuntimeCompletedAt;
  runtime_execution_id?: RuntimeExecutionId;
  started_at: StartedAt1;
}
export interface ProjectionIdentity {
  name: Name;
  strategy: Strategy;
  version: Version;
  [k: string]: unknown;
}
export interface SummaryActivity {
  kind: Kind2;
  label: Label;
  references: EvidenceReferences;
  status?: Status;
  [k: string]: unknown;
}
export interface Truncation {
  [k: string]: TruncationStatus;
}
export interface TruncationStatus {
  total: Total;
  truncated: Truncated;
  [k: string]: unknown;
}
export interface SummaryEvidence {
  label: Label1;
  references: EvidenceReferences;
  status: Status1;
  [k: string]: unknown;
}
