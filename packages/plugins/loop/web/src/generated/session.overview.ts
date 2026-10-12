/* Generated from Core freeze / Loop Pydantic. Do not edit. */

export type Edges = {
  [k: string]: unknown;
}[];
export type EntrypointId = string;
export type ForkOrigin = {
  [k: string]: unknown;
} | null;
export type GraphId = string;
export type LineageRootSessionId = string;
export type RootSessionId = string;
export type AgentName = string | null;
export type AgentPath = string | null;
export type Cwd = string | null;
export type EdgeType = string | null;
export type EndedAt = string | null;
export type FilteredTurnTotal = number;
export type LatestTurnStatus = string | null;
export type Model = string | null;
export type MultiAgentMode = string | null;
export type MultiAgentVersion = string | null;
export type NarrativeTurnTotal = number;
export type ParentInRun = boolean;
export type ParentSessionId = string | null;
export type Relationship = string;
export type RunRootSessionId = string;
export type SessionId = string;
export type SessionRank = number;
export type SourceTurnTotal = number;
export type StartedAt = string | null;
export type Status = string;
export type Title = string | null;
export type Vendor = string;
export type Sessions = OverviewSession[];
export type Cmd = string | null;
export type Commands = string[] | null;
export type Count = number | null;
/**
 * @minItems 1
 */
export type ItemIds = [string, ...string[]];
export type Items = string | null;
export type Kind = "tool_script" | null;
export type Outcome = ("succeeded" | "failed") | null;
export type Path = string | null;
export type PathCounts = {
  [k: string]: number;
} | null;
export type Paths = string[] | null;
export type Queries = string[] | null;
export type Query = string | null;
export type ScriptId = string | null;
export type Session = string | null;
export type Status1 = string | null;
export type Target = string | null;
export type Targets = string[] | null;
export type Task = string | null;
export type Tool = string;
export type Url = string | null;
export type Urls = string[] | null;
export type WrapperStatus = string | null;
export type Activities = OverviewActivity[];
export type ItemId = string;
export type Preview = string;
export type AssistantResponses = OverviewAssistant[];
export type EndedAt1 = string | null;
export type ItemId1 = string;
export type Kind1 = "item";
export type OpenSourceLink = string | null;
export type ItemId2 = string;
export type Language = string;
export type Link = string;
export type LocalOnly = boolean;
export type ProviderCallId = string | null;
export type RecordDigests = string[];
export type RecordIndices = number[];
export type SessionId1 = string;
export type SourceField = string;
export type SourceLog = string;
export type SourceLogSha256 = string;
export type SourceSha256 = string;
export type ToolName = string | null;
export type UnitId = string;
export type FollowingAssistantItemIds = string[];
export type NextItemId = string | null;
export type PrecedingAssistantItemIds = string[];
export type PreviousItemId = string | null;
export type SessionId2 = string;
export type TurnId = string;
export type UserRequestEventId = string | null;
export type ControlItemIds = string[];
export type InventoryCoverage = "complete" | "partial" | "unknown";
export type ItemId3 = string;
export type ItemIds1 = string[];
export type Kind2 = "tool_script";
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
export type Outcome1 = ("succeeded" | "failed" | "unfinished") | null;
export type OwnershipBasis = string;
export type ProviderCallId1 = string | null;
export type ResultCoverage = "recorded" | "not_recorded";
export type ResultRef = {
  [k: string]: unknown;
} | null;
export type SourceToolName = string | null;
export type StartedAt1 = string | null;
export type ToolName1 = string;
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
export type StartedAt2 = string;
export type SchemaVersion = "ct.tool_script.v1";
export type SessionId3 = string;
export type SourceAnalysisCoverage = "partial" | "syntax_errors" | "not_supported";
export type SourceOnlyCalls = ExternalCall[];
export type SourcePreview = string | null;
export type SourcePreviewTruncated = boolean;
export type ToolName2 = string | null;
export type TurnId1 = string;
export type UnitId1 = string;
export type ExecutionUnits = (DirectItemReference | ToolScriptExecution)[];
export type GlobalOrdinal = number;
export type ItemIds2 = string[];
export type UserRequestEventId1 = string | null;
export type SessionId4 = string;
export type SessionNarrativeOrdinal = number;
export type SourceSequence = number;
export type SourceTurnOrdinal = number;
export type StartedAt3 = string | null;
export type Status2 = string;
export type TextTrimmed = boolean;
export type TimestampState = "complete" | "partial" | "missing";
export type TurnId2 = string;
export type Content = string;
export type EventId = string | null;
export type Source = string | null;
export type Turns = OverviewTurn[];

export interface SessionOverviewResponse {
  coverage: Coverage;
  edges: Edges;
  entrypoint_id: EntrypointId;
  fork_origin: ForkOrigin;
  graph_id: GraphId;
  lineage_root_session_id: LineageRootSessionId;
  orchestration: Orchestration;
  project: Project;
  root_session_id: RootSessionId;
  sessions: Sessions;
  totals: Totals;
  turns: Turns;
  [k: string]: unknown;
}
export interface Coverage {
  [k: string]: unknown;
}
export interface Orchestration {
  [k: string]: unknown;
}
export interface Project {
  [k: string]: string | null;
}
export interface OverviewSession {
  agent_name: AgentName;
  agent_path: AgentPath;
  cwd: Cwd;
  edge_type: EdgeType;
  ended_at: EndedAt;
  filtered_turn_total: FilteredTurnTotal;
  latest_turn_status: LatestTurnStatus;
  model: Model;
  multi_agent_mode: MultiAgentMode;
  multi_agent_version: MultiAgentVersion;
  narrative_turn_total: NarrativeTurnTotal;
  parent_in_run: ParentInRun;
  parent_session_id: ParentSessionId;
  relationship: Relationship;
  run_root_session_id: RunRootSessionId;
  session_id: SessionId;
  session_rank: SessionRank;
  source_turn_total: SourceTurnTotal;
  started_at: StartedAt;
  status: Status;
  title: Title;
  vendor: Vendor;
}
export interface Totals {
  [k: string]: number;
}
export interface OverviewTurn {
  activities: Activities;
  assistant_responses: AssistantResponses;
  ended_at: EndedAt1;
  execution_units?: ExecutionUnits;
  global_ordinal: GlobalOrdinal;
  refs: OverviewReferences;
  session_id: SessionId4;
  session_narrative_ordinal: SessionNarrativeOrdinal;
  source_sequence: SourceSequence;
  source_turn_ordinal: SourceTurnOrdinal;
  started_at: StartedAt3;
  status: Status2;
  text_trimmed: TextTrimmed;
  timestamp_state: TimestampState;
  turn_id: TurnId2;
  user_request: OverviewRequestPreview | null;
}
/**
 * One semantic activity cell, not one canonical item.
 *
 * Fields mirror build_overview_flows; descriptions and evidence membership
 * belong to that projector. All semantic activities are included.
 */
export interface OverviewActivity {
  cmd?: Cmd;
  commands?: Commands;
  count?: Count;
  item_ids: ItemIds;
  items?: Items;
  kind?: Kind;
  outcome?: Outcome;
  path?: Path;
  path_counts?: PathCounts;
  paths?: Paths;
  queries?: Queries;
  query?: Query;
  script_id?: ScriptId;
  session?: Session;
  status?: Status1;
  target?: Target;
  targets?: Targets;
  task?: Task;
  tool: Tool;
  url?: Url;
  urls?: Urls;
  wrapper_status?: WrapperStatus;
}
export interface OverviewAssistant {
  item_id: ItemId;
  preview: Preview;
}
export interface DirectItemReference {
  item_id: ItemId1;
  kind?: Kind1;
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
  session_id: SessionId1;
  source_field: SourceField;
  source_log: SourceLog;
  source_log_sha256: SourceLogSha256;
  source_sha256: SourceSha256;
  tool_name?: ToolName;
}
export interface ToolScriptExecution {
  context_refs: ContextReferences;
  control_item_ids?: ControlItemIds;
  inspection_log_ref: InspectionLogRef;
  inventory_coverage: InventoryCoverage;
  item_id: ItemId3;
  item_ids: ItemIds1;
  kind?: Kind2;
  local_only?: LocalOnly1;
  observed_external_calls: ObservedExternalCalls;
  open_source_link: OpenSourceLink1;
  result: ExecutionResult;
  schema_version?: SchemaVersion;
  session_id: SessionId3;
  source: SourceReference;
  source_analysis_coverage?: SourceAnalysisCoverage;
  source_only_calls: SourceOnlyCalls;
  source_preview?: SourcePreview;
  source_preview_truncated?: SourcePreviewTruncated;
  tool_name?: ToolName2;
  turn_id: TurnId1;
  unit_id: UnitId1;
}
export interface ContextReferences {
  following_assistant_item_ids: FollowingAssistantItemIds;
  next_item_id?: NextItemId;
  preceding_assistant_item_ids: PrecedingAssistantItemIds;
  previous_item_id?: PreviousItemId;
  session_id: SessionId2;
  turn_id: TurnId;
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
  outcome?: Outcome1;
  ownership_basis: OwnershipBasis;
  provider_call_id?: ProviderCallId1;
  result_coverage?: ResultCoverage;
  result_ref?: ResultRef;
  source_tool_name?: SourceToolName;
  started_at?: StartedAt1;
  tool_name: ToolName1;
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
  started_at: StartedAt2;
}
export interface OverviewReferences {
  item_ids: ItemIds2;
  user_request_event_id: UserRequestEventId1;
}
export interface OverviewRequestPreview {
  content: Content;
  event_id: EventId;
  source: Source;
}
