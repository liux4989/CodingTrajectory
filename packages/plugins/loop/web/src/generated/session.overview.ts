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
export type Direction = "older";
export type EndOrdinalExclusive = number;
export type HasMore = boolean;
export type NextCursor = string | null;
export type RequestedLimit = number;
export type Returned = number;
export type StartOrdinal = number;
export type Total = number;
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
/**
 * @maxItems 8
 */
export type Activities =
  | []
  | [OverviewActivity]
  | [OverviewActivity, OverviewActivity]
  | [OverviewActivity, OverviewActivity, OverviewActivity]
  | [OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity]
  | [OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity]
  | [OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity, OverviewActivity]
  | [
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity
    ]
  | [
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity,
      OverviewActivity
    ];
export type Concept = string | null;
export type ExitCode = number | null;
export type ItemId = string;
export type Kind = string;
export type Operation = string | null;
export type Outcome = string | null;
export type Path = string | null;
export type Status1 = string | null;
export type Target = string | null;
export type TargetKind = string | null;
export type ToolName = string | null;
/**
 * @maxItems 8
 */
export type AssistantResponses =
  | []
  | [OverviewAssistant]
  | [OverviewAssistant, OverviewAssistant]
  | [OverviewAssistant, OverviewAssistant, OverviewAssistant]
  | [OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant]
  | [OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant]
  | [OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant, OverviewAssistant]
  | [
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant
    ]
  | [
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant,
      OverviewAssistant
    ];
export type ItemId1 = string;
export type Preview = string;
export type Returned1 = number;
export type Total1 = number;
export type Truncated = boolean;
export type TextTrimmed = boolean;
export type EndedAt1 = string | null;
export type GlobalOrdinal = number;
/**
 * @maxItems 100
 */
export type ItemIds = string[];
export type UserRequestEventId = string | null;
export type SessionId1 = string;
export type SessionNarrativeOrdinal = number;
export type SourceSequence = number;
export type SourceTurnOrdinal = number;
export type StartedAt1 = string | null;
export type Status2 = string;
export type TimestampState = "complete" | "partial" | "missing";
export type TurnId = string;
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
  page: OverviewPage;
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
export interface OverviewPage {
  direction?: Direction;
  end_ordinal_exclusive: EndOrdinalExclusive;
  has_more: HasMore;
  next_cursor: NextCursor;
  requested_limit: RequestedLimit;
  returned: Returned;
  start_ordinal: StartOrdinal;
  total: Total;
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
  content_coverage: TurnContentCoverage;
  ended_at: EndedAt1;
  global_ordinal: GlobalOrdinal;
  refs: OverviewReferences;
  session_id: SessionId1;
  session_narrative_ordinal: SessionNarrativeOrdinal;
  source_sequence: SourceSequence;
  source_turn_ordinal: SourceTurnOrdinal;
  started_at: StartedAt1;
  status: Status2;
  timestamp_state: TimestampState;
  turn_id: TurnId;
  user_request: OverviewRequestPreview | null;
}
export interface OverviewActivity {
  concept: Concept;
  exit_code: ExitCode;
  item_id: ItemId;
  kind: Kind;
  operation: Operation;
  outcome: Outcome;
  path: Path;
  status: Status1;
  target: Target;
  target_kind: TargetKind;
  tool_name: ToolName;
}
export interface OverviewAssistant {
  item_id: ItemId1;
  preview: Preview;
}
export interface TurnContentCoverage {
  activities: ContentCount;
  assistant_responses: ContentCount;
  item_ids: ContentCount;
  text_trimmed: TextTrimmed;
}
export interface ContentCount {
  returned: Returned1;
  total: Total1;
  truncated: Truncated;
}
export interface OverviewReferences {
  item_ids: ItemIds;
  user_request_event_id: UserRequestEventId;
}
export interface OverviewRequestPreview {
  content: Content;
  event_id: EventId;
  source: Source;
}
