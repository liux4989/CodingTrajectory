"""Strict, bounded historical artifact shared by local and remote APIs.

The artifact is a semantic projection, not a second canonical trajectory. Raw
events and transcript/tool bodies never enter the model. Existing historical
handlers consume an ephemeral canonical graph reconstructed from these facts,
so local and remote shareable reads retain one implementation.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from pathlib import Path, PurePath
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.analysis.content_size import (
    CONTENT_SIZE_MEASUREMENT_KEY,
    event_text_size,
    tool_input_summary,
)
from coding_trajectory.analysis.measurements import (
    extract_item_measurements,
    extract_session_measurements,
)
from coding_trajectory.analysis.request_lineage import extract_user_request
from coding_trajectory.analysis.tool_summary import summarize_tool_call
from coding_trajectory.analysis.tool_summary_shell import classify_verification_command
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.discovery import (
    DiscoveryCandidate,
    merge_session_segments,
    stabilize_session,
)
from coding_trajectory.ingestion.graph import (
    build_session_graph,
    canonical_spawn_origins,
)
from coding_trajectory.ingestion.indexes import (
    build_session_graph_index,
    event_for_turn_user_request,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    CanonicalSpawnOrigin,
    ClaudeCodeExtensions,
    CommandExecutionItem,
    ContextCategoryObservation,
    ContextSourceMeasurement,
    ContextUsageObservation,
    Event,
    EventTextMeasurement,
    EventType,
    FileChangeItem,
    Item,
    ItemMeasurements,
    PlanItem,
    ReasoningItem,
    RuntimeObservation,
    Session,
    SessionEdge,
    SessionGraph,
    SessionGraphSummary,
    SessionMeasurements,
    TeamMemberState,
    TeamTaskState,
    TeamTurnState,
    ToolCallItem,
    Turn,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.token_counter import counter_for_session_graph, scoped_counter


SHAREABLE_GRAPH_SCHEMA_VERSION = "ct.shareable_graph.v1"
MAX_SHAREABLE_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_SHAREABLE_PUBLICATION_BYTES = 16 * 1024 * 1024
_SYNTHETIC_REQUEST_NAMESPACE = uuid5(
    NAMESPACE_URL, "codingtrajectory:shareable-request"
)
_ITEM_KINDS = Literal[
    "agent_message",
    "tool_call",
    "command_execution",
    "file_change",
    "reasoning",
    "plan",
]
_BoundedString = Annotated[str, Field(max_length=512)]
_Preview = Annotated[str, Field(max_length=280)]
_CostText = Annotated[
    str,
    Field(
        max_length=64,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$",
    ),
]
_CONTEXT_SOURCE_LABELS = {
    "base_system": frozenset({"Base instructions", "System prompt & tools"}),
    "developer_instructions": frozenset({"Developer instructions"}),
    "agents_md": frozenset({"AGENTS.md"}),
    "skills": frozenset({"Skills"}),
    "mcp": frozenset({"Tools / MCP"}),
    "memory": frozenset({"Memory"}),
}
_BASE64_BODY = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_HOST_PATH = re.compile(
    r"^(?:~/|/Users/|/home/|/root/|/private/|/tmp/|/var/|/Volumes/|"
    r"/workspace/|/workspaces/|/mnt/|/srv/|/opt/|[A-Za-z]:[\\/])"
)


class ShareableModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShareableUsage(ShareableModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    # JSONB normalizes numeric spellings, while Python's float encoder can use
    # exponents. Preserve the exact finite float spelling as a decimal string
    # so Python and PostgreSQL hash identical canonical bytes.
    cost_usd: _CostText | None = None


class ShareableUsageCategory(ShareableModel):
    key: _BoundedString
    label: _BoundedString
    tokens: int = Field(ge=0)
    confidence: _BoundedString
    source: _BoundedString | None = None


class ShareableRequestUsage(ShareableModel):
    request_id: UUID
    timestamp: datetime
    source: _BoundedString
    model: _BoundedString | None = None
    provider: _BoundedString | None = None
    context_window_tokens: int | None = Field(default=None, ge=0)
    used_input_tokens: int = Field(default=0, ge=0)
    usage: ShareableUsage = Field(default_factory=ShareableUsage)
    categories: list[ShareableUsageCategory] = Field(
        default_factory=list, max_length=32
    )


class ShareableContextSourceMeasurement(ShareableModel):
    timestamp: datetime
    key: _BoundedString
    label: _BoundedString
    reported_tokens: int | None = Field(default=None, ge=0)
    chars: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class ShareableEventTextMeasurement(ShareableModel):
    timestamp: datetime
    chars: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class ShareableSessionMeasurements(ShareableModel):
    context_sources: list[ShareableContextSourceMeasurement] = Field(
        default_factory=list
    )
    llm_response_count: int = Field(default=0, ge=0)
    llm_response_text_sizes: list[ShareableEventTextMeasurement] = Field(
        default_factory=list
    )


class ShareableToolSummary(ShareableModel):
    name: _BoundedString
    description: Literal["tests", "checks", "command"] | None = None
    status: _BoundedString | None = None
    optimization_profile: _BoundedString | None = None
    activity_hidden: bool | None = None
    activity_kind: _BoundedString | None = None
    activity_source: _BoundedString | None = None
    activity_outcome: _BoundedString | None = None
    activity_fidelity: _BoundedString | None = None
    activity_wrapper_status: _BoundedString | None = None


class ShareableItemMeasurements(ShareableModel):
    input_chars: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_chars: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    text_chars: int = Field(default=0, ge=0)
    text_tokens: int = Field(default=0, ge=0)
    projection_only: bool = False
    output_truncated: bool = False
    output_original_tokens: int | None = Field(default=None, ge=0)
    text_preview: None = None
    tool_summary: ShareableToolSummary | None = None


class ShareableItemSemantic(ShareableModel):
    verification_kind: _BoundedString | None = None
    resolution_key: _BoundedString | None = None
    plan_actions: list[_Preview] = Field(default_factory=list, max_length=0)


class ShareableItem(ShareableModel):
    item_id: UUID
    sequence: int = Field(ge=0)
    kind: _ITEM_KINDS
    started_at: datetime
    completed_at: datetime | None = None
    status: _BoundedString | None = None
    tool_name: _BoundedString | None = None
    tool_category: _BoundedString | None = None
    operation: _BoundedString | None = None
    exit_code: int | None = None
    path: _BoundedString | None = None
    measurements: ShareableItemMeasurements = Field(
        default_factory=ShareableItemMeasurements
    )
    semantic: ShareableItemSemantic = Field(default_factory=ShareableItemSemantic)


class ShareableUserRequest(ShareableModel):
    request_id: UUID
    type: Literal["message", "command"] = "message"
    source: _BoundedString = "human_user"
    content: Literal["[content omitted]"] = "[content omitted]"
    chars: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_measurement(self) -> ShareableUserRequest:
        if (self.chars is None) != (self.tokens is None):
            raise ValueError("shareable user-request measurement is incomplete")
        return self


class ShareableTeamMember(ShareableModel):
    member_id: _BoundedString
    session_id: UUID | None = None
    agent_type: _BoundedString | None = None


class ShareableTeamTask(ShareableModel):
    task_id: _BoundedString
    status: _BoundedString | None = None
    member_id: _BoundedString | None = None
    blocked_by: list[_BoundedString] = Field(default_factory=list, max_length=64)


class ShareableTeamState(ShareableModel):
    members: list[ShareableTeamMember] = Field(default_factory=list)
    tasks: list[ShareableTeamTask] = Field(default_factory=list)


class ShareableTurn(ShareableModel):
    turn_id: UUID
    sequence: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime | None = None
    status: _BoundedString
    user_request: ShareableUserRequest | None = None
    requests: list[ShareableRequestUsage] = Field(default_factory=list)
    items: list[ShareableItem] = Field(default_factory=list)
    team_state: ShareableTeamState | None = None


class ShareableRuntimeObservation(ShareableModel):
    timestamp: datetime
    kind: _BoundedString
    duration_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    num_turns: int | None = Field(default=None, ge=0)
    pre_tokens: int | None = Field(default=None, ge=0)
    post_tokens: int | None = Field(default=None, ge=0)
    cumulative_dropped_tokens: int | None = Field(default=None, ge=0)
    effort_from: _BoundedString | None = None
    effort_to: _BoundedString | None = None


class ShareableSpawnOrigin(ShareableModel):
    target_session_id: UUID
    turn_id: UUID | None = None
    item_id: UUID | None = None
    tool_name: _BoundedString | None = None


class ShareableSessionTopology(ShareableModel):
    sidechain: bool = False
    forked: bool = False
    spawned: bool = False
    spawn_depth: int | None = Field(default=None, ge=0)
    multi_agent_version: _BoundedString | None = None
    multi_agent_mode: _BoundedString | None = None
    spawn_origins: list[ShareableSpawnOrigin] = Field(default_factory=list)


class ShareableSession(ShareableModel):
    session_id: UUID
    parent_session_id: UUID | None = None
    vendor: Vendor
    started_at: datetime
    ended_at: datetime | None = None
    status: _BoundedString
    model: _BoundedString | None = None
    reasoning_effort: _BoundedString | None = None
    title: None = None
    preview: None = None
    topology: ShareableSessionTopology = Field(default_factory=ShareableSessionTopology)
    runtime: list[ShareableRuntimeObservation] = Field(default_factory=list)
    measurements: ShareableSessionMeasurements = Field(
        default_factory=ShareableSessionMeasurements
    )
    turns: list[ShareableTurn] = Field(default_factory=list)


class ShareableEdgeOrigin(ShareableModel):
    session_id: UUID
    turn_id: UUID | None = None
    item_id: UUID | None = None


class ShareableEdge(ShareableModel):
    source_session_id: UUID
    target_session_id: UUID
    kind: Literal[
        "spawned_subagent",
        "sidechain_of",
        "forked_from",
        "handoff_to",
        "resumed_from",
        "teammate_of",
    ]
    origin: ShareableEdgeOrigin
    tool_name: _BoundedString | None = None
    provenance: Literal["observed", "derived"] = "derived"
    confidence: Literal["high", "medium", "low"] = "medium"


class ShareableGraphSummary(ShareableModel):
    root_session_id: UUID
    project: _BoundedString | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: _BoundedString | None = None
    session_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    item_count: int = Field(ge=0)


class ShareableCoverage(ShareableModel):
    content: Literal[False] = False
    events: Literal[False] = False
    topology: Literal[True] = True
    usage: Literal[True] = True
    measurements: Literal[True] = True
    semantic_previews: Literal[False] = False


class ShareableGraphArtifact(ShareableModel):
    schema_version: Literal["ct.shareable_graph.v1"] = SHAREABLE_GRAPH_SCHEMA_VERSION
    graph: ShareableGraphSummary
    sessions: list[ShareableSession]
    edges: list[ShareableEdge] = Field(default_factory=list)
    coverage: ShareableCoverage = Field(default_factory=ShareableCoverage)

    @model_validator(mode="after")
    def validate_boundary(self) -> ShareableGraphArtifact:
        if not self.sessions:
            raise ValueError("shareable graph requires at least one session")
        session_ids = {session.session_id for session in self.sessions}
        if len(session_ids) != len(self.sessions):
            raise ValueError("shareable graph contains duplicate sessions")
        if self.graph.root_session_id not in session_ids:
            raise ValueError("shareable graph root is not retained")
        if self.graph.session_count != len(self.sessions):
            raise ValueError("shareable graph session count mismatch")
        turn_count = sum(len(session.turns) for session in self.sessions)
        item_count = sum(
            len(turn.items) for session in self.sessions for turn in session.turns
        )
        if self.graph.turn_count != turn_count or self.graph.item_count != item_count:
            raise ValueError("shareable graph hierarchy count mismatch")

        turn_owners: dict[UUID, UUID] = {}
        item_owners: dict[UUID, tuple[UUID, UUID]] = {}
        for session in self.sessions:
            turn_sequences = [turn.sequence for turn in session.turns]
            if turn_sequences != sorted(turn_sequences) or len(turn_sequences) != len(
                set(turn_sequences)
            ):
                raise ValueError("shareable graph turn ordering is invalid")
            for turn in session.turns:
                if turn.turn_id in turn_owners:
                    raise ValueError("shareable graph contains duplicate turns")
                turn_owners[turn.turn_id] = session.session_id
                item_sequences = [item.sequence for item in turn.items]
                if item_sequences != sorted(item_sequences) or len(
                    item_sequences
                ) != len(set(item_sequences)):
                    raise ValueError("shareable graph item ordering is invalid")
                for item in turn.items:
                    if item.item_id in item_owners:
                        raise ValueError("shareable graph contains duplicate items")
                    item_owners[item.item_id] = (session.session_id, turn.turn_id)
            for origin in session.topology.spawn_origins:
                if (
                    origin.turn_id is not None
                    and turn_owners.get(origin.turn_id) != session.session_id
                ):
                    raise ValueError("shareable graph spawn turn ownership mismatch")
                if origin.item_id is not None:
                    if origin.turn_id is None or item_owners.get(origin.item_id) != (
                        session.session_id,
                        origin.turn_id,
                    ):
                        raise ValueError(
                            "shareable graph spawn item ownership mismatch"
                        )

        edge_identities: set[tuple[str, UUID, UUID, UUID | None, UUID | None]] = set()
        for edge in self.edges:
            if (
                edge.source_session_id not in session_ids
                or edge.target_session_id not in session_ids
                or edge.origin.session_id != edge.source_session_id
            ):
                raise ValueError("shareable graph edge endpoint mismatch")
            if (
                edge.origin.turn_id is not None
                and turn_owners.get(edge.origin.turn_id) != edge.source_session_id
            ):
                raise ValueError("shareable graph edge turn ownership mismatch")
            if edge.origin.item_id is not None:
                if edge.origin.turn_id is None or item_owners.get(
                    edge.origin.item_id
                ) != (
                    edge.source_session_id,
                    edge.origin.turn_id,
                ):
                    raise ValueError("shareable graph edge item ownership mismatch")
            identity = (
                edge.kind,
                edge.source_session_id,
                edge.target_session_id,
                edge.origin.turn_id,
                edge.origin.item_id,
            )
            if identity in edge_identities:
                raise ValueError("shareable graph contains duplicate edges")
            edge_identities.add(identity)
        payload = self.model_dump(mode="json", exclude_none=True)
        _reject_embedded_content(payload)
        encoded = canonical_json(payload).encode()
        if len(encoded) > MAX_SHAREABLE_ARTIFACT_BYTES:
            raise ValueError("shareable graph exceeds the 8 MiB artifact bound")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json", exclude_none=True)).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def to_session_graph(self) -> SessionGraph:
        sessions = [_to_session(session) for session in self.sessions]
        edges = [_to_edge(edge) for edge in self.edges]
        return SessionGraph(
            root_session_id=self.graph.root_session_id,
            project_identifier=self.graph.project,
            summary=SessionGraphSummary(
                root_session_id=self.graph.root_session_id,
                started_at=self.graph.started_at,
                ended_at=self.graph.ended_at,
                session_count=self.graph.session_count,
                turn_count=self.graph.turn_count,
                vendors=sorted(
                    {session.vendor for session in sessions},
                    key=lambda vendor: vendor.value,
                ),
            ),
            edges=edges,
            sessions=sessions,
        )


def build_shareable_graph_artifact(
    session_graph: SessionGraph,
) -> ShareableGraphArtifact:
    """Project one canonical graph onto the bounded shared API source."""

    index = build_session_graph_index(session_graph)
    with scoped_counter(counter_for_session_graph(session_graph)):
        sessions = [
            _build_shareable_session(session, index=index)
            for session in session_graph.sessions
        ]
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = max(
        (session.ended_at for session in sessions if session.ended_at is not None),
        default=None,
    )
    root = next(
        (
            session
            for session in sessions
            if session.session_id == session_graph.root_session_id
        ),
        None,
    )
    return ShareableGraphArtifact(
        graph=ShareableGraphSummary(
            root_session_id=session_graph.root_session_id,
            project=_portable_project(session_graph.project_identifier),
            started_at=started_at,
            ended_at=ended_at,
            status=root.status if root is not None else None,
            session_count=len(sessions),
            turn_count=sum(len(session.turns) for session in sessions),
            item_count=sum(
                len(turn.items) for session in sessions for turn in session.turns
            ),
        ),
        sessions=sessions,
        edges=[_build_shareable_edge(edge) for edge in session_graph.edges],
    )


def build_shareable_segments(
    segments: list[
        tuple[DiscoveryCandidate, Path, list[dict[str, Any]], set[str] | None]
    ],
) -> ShareableGraphArtifact:
    """Build one logical source artifact from exactly fenced source records."""

    canonical_segments: list[tuple[Path, Session]] = []
    for candidate, source, records, parent_started_turn_ids in segments:
        session = candidate.adapter_cls().build_canonical_session(
            source,
            records,
            parent_started_turn_ids=parent_started_turn_ids,
        )
        canonical_segments.append(
            (
                source,
                stabilize_session(
                    session,
                    vendor=candidate.vendor,
                    source=source,
                ),
            )
        )
    canonical_segments.sort(key=lambda entry: (entry[1].started_at, str(entry[0])))
    session = (
        canonical_segments[0][1]
        if len(canonical_segments) == 1
        else merge_session_segments(canonical_segments)
    )
    graph = build_session_graph(
        root_session_id=session.session_id,
        project_identifier="shareable-source",
        sessions=[session],
    )
    return build_shareable_graph_artifact(graph)


def shareable_session_graph(session_graph: SessionGraph) -> SessionGraph:
    """Round-trip a graph through the exact artifact used by remote reads."""

    return build_shareable_graph_artifact(session_graph).to_session_graph()


def _build_shareable_session(session: Session, *, index: Any) -> ShareableSession:
    measurements = session.measurements or extract_session_measurements(session)
    origins = _canonical_spawn_origins(session)
    return ShareableSession(
        session_id=session.session_id,
        parent_session_id=session.parent_session_id,
        vendor=session.vendor,
        started_at=session.started_at,
        ended_at=session.ended_at,
        status=session.status.value,
        model=session.model,
        reasoning_effort=session.reasoning_effort,
        title=None,
        preview=None,
        topology=_build_topology(session, origins),
        runtime=[
            ShareableRuntimeObservation(
                timestamp=observation.timestamp,
                kind=observation.kind,
                duration_ms=observation.duration_ms,
                time_to_first_token_ms=observation.time_to_first_token_ms,
                num_turns=observation.num_turns,
                pre_tokens=observation.pre_tokens,
                post_tokens=observation.post_tokens,
                cumulative_dropped_tokens=observation.cumulative_dropped_tokens,
                effort_from=observation.effort_from,
                effort_to=observation.effort_to,
            )
            for observation in session.runtime_observations
        ],
        measurements=_build_session_measurements(measurements),
        turns=[
            _build_shareable_turn(turn, session=session, index=index)
            for turn in session.turns
        ],
    )


def _build_shareable_turn(turn: Turn, *, session: Session, index: Any) -> ShareableTurn:
    event_ids = set(turn.event_ids)
    request = extract_user_request(index, turn, session=session)
    return ShareableTurn(
        turn_id=turn.turn_id,
        sequence=turn.sequence,
        started_at=turn.started_at,
        completed_at=turn.ended_at,
        status=turn.status.value,
        user_request=_build_user_request(turn, request, index=index),
        requests=[
            _build_request_usage(observation)
            for observation in session.context_usage
            if observation.source_event_id in event_ids
            and observation.source_event_id is not None
        ],
        items=[_build_shareable_item(item) for item in turn.items],
        team_state=_build_team_state(turn.team_state),
    )


def _build_user_request(
    turn: Turn, request: dict[str, str] | None, *, index: Any
) -> ShareableUserRequest | None:
    if request is None or not request.get("content", "").strip():
        return None
    content = "[content omitted]"
    request_id = turn.user_request_event_id or uuid5(
        _SYNTHETIC_REQUEST_NAMESPACE, str(turn.turn_id)
    )
    source_event = event_for_turn_user_request(index, turn)
    text_size = event_text_size(source_event) if source_event is not None else None
    request_type = request.get("type")
    return ShareableUserRequest(
        request_id=request_id,
        type=request_type if request_type in {"message", "command"} else "message",
        source=request.get("source") or "human_user",
        content=content,
        chars=text_size.chars if text_size is not None else None,
        tokens=text_size.tokens if text_size is not None else None,
    )


def _build_request_usage(observation: ContextUsageObservation) -> ShareableRequestUsage:
    usage = observation.usage
    return ShareableRequestUsage(
        request_id=observation.source_event_id,
        timestamp=observation.timestamp,
        source=observation.source,
        model=observation.model,
        provider=observation.provider,
        context_window_tokens=observation.context_window_tokens,
        used_input_tokens=observation.used_input_tokens,
        usage=ShareableUsage(
            input_tokens=_usage_int(usage, "input_tokens", "inputTokens"),
            cached_input_tokens=_usage_int(
                usage, "cached_input_tokens", "cachedInputTokens"
            ),
            cache_creation_input_tokens=_usage_int(
                usage,
                "cache_creation_input_tokens",
                "cacheCreationInputTokens",
            ),
            output_tokens=_usage_int(usage, "output_tokens", "outputTokens"),
            reasoning_output_tokens=_usage_int(
                usage, "reasoning_output_tokens", "reasoningOutputTokens"
            ),
            total_tokens=_usage_int(usage, "total_tokens", "totalTokens"),
            uncached_input_tokens=_usage_optional_int(
                usage, "uncached_input_tokens", "uncachedInputTokens"
            ),
            cost_usd=_cost_text(_usage_optional_float(usage, "cost_usd", "costUsd")),
        ),
        categories=[
            ShareableUsageCategory(
                key=category.key,
                label=category.label,
                tokens=category.tokens,
                confidence=category.confidence,
                source=category.source,
            )
            for category in observation.categories
        ],
    )


def _build_shareable_item(item: Item) -> ShareableItem:
    measurements = item.measurements or extract_item_measurements(item)
    tool_summary = _bounded_tool_summary(item, measurements)
    semantic = _item_semantic(item, tool_summary)
    return ShareableItem(
        item_id=item.item_id,
        sequence=item.sequence,
        kind=item.kind,
        started_at=item.started_at,
        completed_at=item.completed_at,
        status=(
            str(getattr(item.status, "value", item.status))
            if item.status is not None
            else None
        ),
        tool_name=_bounded(getattr(item, "tool_name", None)),
        tool_category=tool_summary.name if tool_summary is not None else None,
        operation=_bounded(getattr(item, "operation", None)),
        exit_code=getattr(item, "exit_code", None),
        path=(_portable_path(item.path) if isinstance(item, FileChangeItem) else None),
        measurements=ShareableItemMeasurements(
            input_chars=measurements.input_chars,
            input_tokens=measurements.input_tokens,
            output_chars=measurements.output_chars,
            output_tokens=measurements.output_tokens,
            text_chars=measurements.text_chars,
            text_tokens=measurements.text_tokens,
            projection_only=measurements.projection_only,
            output_truncated=measurements.output_truncated,
            output_original_tokens=measurements.output_original_tokens,
            text_preview=None,
            tool_summary=tool_summary,
        ),
        semantic=semantic,
    )


def _bounded_tool_summary(
    item: Item, measurements: ItemMeasurements
) -> ShareableToolSummary | None:
    raw = measurements.tool_summary or summarize_tool_call(item)
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    name = _bounded(raw["name"])
    if not name:
        return None
    description = raw.get("description")
    if name == "RunCommand":
        verification = (
            classify_verification_command(item.command)
            if isinstance(item, CommandExecutionItem)
            else None
        )
        retained_description = (
            description if description in {"tests", "checks", "command"} else None
        )
        description = verification or retained_description or "command"
    elif name in {"WebSearch", "WebFetch"}:
        description = None
    elif isinstance(description, str):
        description = _portable_path(description)
    else:
        description = None
    return ShareableToolSummary(
        name=name,
        description=description
        if description in {"tests", "checks", "command"}
        else None,
        status=_bounded(raw.get("status")),
        optimization_profile=_bounded(raw.get("optimization_profile")),
        activity_hidden=(
            raw.get("activity_hidden")
            if isinstance(raw.get("activity_hidden"), bool)
            else None
        ),
        activity_kind=_bounded(raw.get("activity_kind")),
        activity_source=_bounded(raw.get("activity_source")),
        activity_outcome=_bounded(raw.get("activity_outcome")),
        activity_fidelity=_bounded(raw.get("activity_fidelity")),
        activity_wrapper_status=_bounded(raw.get("activity_wrapper_status")),
    )


def _item_semantic(
    item: Item, tool_summary: ShareableToolSummary | None
) -> ShareableItemSemantic:
    vendor_data = item.vendor_data
    retained = (
        vendor_data.get("shareable_semantics")
        if isinstance(vendor_data, dict)
        and isinstance(vendor_data.get("shareable_semantics"), dict)
        else {}
    )
    verification_kind = (
        classify_verification_command(item.command)
        if isinstance(item, CommandExecutionItem)
        else None
    ) or _bounded(retained.get("verification_kind"))
    resolution_key: str | None = _bounded(retained.get("resolution_key"))
    if isinstance(item, FileChangeItem) and item.path:
        resolution_key = f"file:{_portable_path(item.path)}"
    elif isinstance(item, CommandExecutionItem):
        summary = tool_input_summary(item.command)
        if summary:
            resolution_key = "command:" + hashlib.sha256(summary.encode()).hexdigest()
    elif tool_summary is not None:
        resolution_key = f"tool:{tool_summary.name}"
    return ShareableItemSemantic(
        verification_kind=_bounded(verification_kind),
        resolution_key=_bounded(resolution_key),
        plan_actions=[],
    )


def _build_session_measurements(
    measurements: SessionMeasurements,
) -> ShareableSessionMeasurements:
    return ShareableSessionMeasurements(
        context_sources=[
            _build_context_source(source) for source in measurements.context_sources
        ],
        llm_response_count=measurements.llm_response_count,
        llm_response_text_sizes=[
            ShareableEventTextMeasurement(
                timestamp=value.timestamp,
                chars=value.chars,
                tokens=value.tokens,
            )
            for value in measurements.llm_response_text_sizes
        ],
    )


def _build_context_source(
    source: ContextSourceMeasurement,
) -> ShareableContextSourceMeasurement:
    labels = _CONTEXT_SOURCE_LABELS.get(source.key)
    if labels is not None and source.label in labels:
        key = source.key
        label = source.label
    else:
        key = "other_context"
        label = "Other context"
    return ShareableContextSourceMeasurement(
        timestamp=source.timestamp,
        key=key,
        label=label,
        reported_tokens=source.reported_tokens,
        chars=source.chars,
        tokens=source.tokens,
    )


def _build_team_state(value: TeamTurnState | None) -> ShareableTeamState | None:
    if value is None:
        return None
    state = ShareableTeamState(
        members=[
            ShareableTeamMember(
                member_id=_bounded(member.member_id) or "member",
                session_id=member.session_id,
                agent_type=_bounded(member.agent_type),
            )
            for member in value.members
        ],
        tasks=[
            ShareableTeamTask(
                task_id=_bounded(task.task_id) or "task",
                status=_bounded(task.status),
                member_id=_bounded(task.member_id),
                blocked_by=[_bounded(item) or "task" for item in task.blocked_by],
            )
            for task in value.tasks
        ],
    )
    return state if state.members or state.tasks else None


def _build_topology(
    session: Session, origins: dict[str, CanonicalSpawnOrigin]
) -> ShareableSessionTopology:
    extensions = session.extensions
    claude = extensions.claude_code if extensions else None
    codex = extensions.codex if extensions else None
    return ShareableSessionTopology(
        sidechain=bool(claude and claude.is_sidechain),
        forked=bool(codex and codex.forked_from_id),
        spawned=bool(codex and codex.spawn_parent_thread_id),
        spawn_depth=(
            codex.spawn_depth
            if codex and codex.spawn_depth is not None
            else claude.spawn_depth
            if claude
            else None
        ),
        multi_agent_version=_bounded(codex.multi_agent_version if codex else None),
        multi_agent_mode=_bounded(codex.multi_agent_mode if codex else None),
        spawn_origins=[
            ShareableSpawnOrigin(
                target_session_id=UUID(child_id),
                turn_id=origin.turn_id,
                item_id=origin.item_id,
                tool_name=_bounded(origin.tool_name),
            )
            for child_id, origin in sorted(origins.items())
        ],
    )


def _canonical_spawn_origins(session: Session) -> dict[str, CanonicalSpawnOrigin]:
    extensions = session.extensions
    existing = (
        dict(extensions.codex.canonical_spawn_origins)
        if extensions and extensions.codex
        else {}
    )
    return {**existing, **canonical_spawn_origins(session)}


def _build_shareable_edge(edge: SessionEdge) -> ShareableEdge:
    tool_name = None
    if isinstance(edge.metadata, dict):
        tool_name = _bounded(edge.metadata.get("tool_name"))
    return ShareableEdge(
        source_session_id=edge.source_session_id,
        target_session_id=edge.target_session_id,
        kind=edge.type,
        origin=ShareableEdgeOrigin(
            session_id=edge.source_session_id,
            turn_id=edge.source_turn_id,
            item_id=edge.source_item_id,
        ),
        tool_name=tool_name,
        provenance=edge.provenance,
        confidence=edge.confidence,
    )


def _to_session(value: ShareableSession) -> Session:
    events: list[Event] = []
    turns: list[Turn] = []
    context_usage: list[ContextUsageObservation] = []
    for turn in value.turns:
        event_ids = [request.request_id for request in turn.requests]
        user_request_event_id = None
        if turn.user_request is not None:
            user_request_event_id = turn.user_request.request_id
            event_ids.insert(0, user_request_event_id)
            payload_key = (
                "team_request_summary"
                if turn.user_request.source in {"team_lead", "parent_agent"}
                else "text"
            )
            request_text = (
                f"<command-name>{turn.user_request.content}</command-name>"
                if turn.user_request.type == "command"
                else turn.user_request.content
            )
            request_measurement = (
                {
                    CONTENT_SIZE_MEASUREMENT_KEY: {
                        "chars": turn.user_request.chars,
                        "tokens": turn.user_request.tokens,
                    }
                }
                if turn.user_request.chars is not None
                and turn.user_request.tokens is not None
                else {}
            )
            events.append(
                Event(
                    event_id=user_request_event_id,
                    session_id=value.session_id,
                    timestamp=turn.started_at,
                    type=EventType.USER_PROMPT_SUBMITTED,
                    vendor_source=value.vendor,
                    payload={payload_key: request_text, **request_measurement},
                )
            )
        for request in turn.requests:
            usage = request.usage.model_dump(mode="json", exclude_none=True)
            if request.usage.cost_usd is not None:
                usage["cost_usd"] = float(request.usage.cost_usd)
            context_usage.append(
                ContextUsageObservation(
                    source_event_id=request.request_id,
                    timestamp=request.timestamp,
                    source=request.source,
                    model=request.model,
                    provider=request.provider,
                    context_window_tokens=request.context_window_tokens,
                    used_input_tokens=request.used_input_tokens,
                    usage=usage,
                    categories=[
                        ContextCategoryObservation(**category.model_dump(mode="python"))
                        for category in request.categories
                    ],
                )
            )
        turns.append(
            Turn(
                turn_id=turn.turn_id,
                session_id=value.session_id,
                sequence=turn.sequence,
                started_at=turn.started_at,
                ended_at=turn.completed_at,
                user_request_event_id=user_request_event_id,
                event_ids=list(dict.fromkeys(event_ids)),
                items=[
                    _to_item(item, value.session_id, turn.turn_id)
                    for item in turn.items
                ],
                team_state=_to_team_state(turn.team_state),
                status=turn.status,
            )
        )
    measurements = SessionMeasurements(
        context_sources=[
            ContextSourceMeasurement(**source.model_dump(mode="python"))
            for source in value.measurements.context_sources
        ],
        llm_response_count=value.measurements.llm_response_count,
        llm_response_text_sizes=[
            EventTextMeasurement(**entry.model_dump(mode="python"))
            for entry in value.measurements.llm_response_text_sizes
        ],
    )
    return Session(
        session_id=value.session_id,
        vendor=value.vendor,
        model=value.model,
        reasoning_effort=value.reasoning_effort,
        started_at=value.started_at,
        ended_at=value.ended_at,
        parent_session_id=value.parent_session_id,
        events=events,
        turns=turns,
        context_usage=context_usage,
        runtime_observations=[
            RuntimeObservation(**entry.model_dump(mode="python"))
            for entry in value.runtime
        ],
        measurements=measurements,
        extensions=_to_extensions(value),
        status=value.status,
    )


def _to_item(value: ShareableItem, session_id: UUID, turn_id: UUID) -> Item:
    measurements = ItemMeasurements(
        **value.measurements.model_dump(mode="python", exclude={"tool_summary"}),
        tool_summary=(
            value.measurements.tool_summary.model_dump(mode="python", exclude_none=True)
            if value.measurements.tool_summary is not None
            else None
        ),
    )
    common: dict[str, Any] = {
        "item_id": value.item_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "sequence": value.sequence,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "status": value.status,
        "event_ids": [],
        "measurements": measurements,
        "vendor_data": {
            "shareable_semantics": value.semantic.model_dump(
                mode="json", exclude_none=True
            )
        },
    }
    if value.kind == "agent_message":
        return AgentMessageItem(**common)
    if value.kind == "reasoning":
        return ReasoningItem(**common)
    if value.kind == "command_execution":
        return CommandExecutionItem(
            **common,
            tool_name=value.tool_name,
            exit_code=value.exit_code,
        )
    if value.kind == "file_change":
        return FileChangeItem(
            **common,
            tool_name=value.tool_name,
            path=value.path,
            operation=value.operation,
        )
    if value.kind == "plan":
        return PlanItem(**common, tool_name=value.tool_name)
    return ToolCallItem(**common, tool_name=value.tool_name)


def _to_team_state(value: ShareableTeamState | None) -> TeamTurnState | None:
    if value is None:
        return None
    return TeamTurnState(
        members=[
            TeamMemberState(**entry.model_dump(mode="python"))
            for entry in value.members
        ],
        tasks=[
            TeamTaskState(**entry.model_dump(mode="python")) for entry in value.tasks
        ],
    )


def _to_extensions(value: ShareableSession) -> VendorExtensions | None:
    topology = value.topology
    claude = (
        ClaudeCodeExtensions(
            title=value.title,
            is_sidechain=topology.sidechain,
            spawn_depth=topology.spawn_depth,
        )
        if value.vendor == Vendor.CLAUDE_CODE
        else None
    )
    codex = None
    pi = None
    if value.vendor == Vendor.CODEX_CLI:
        from coding_trajectory.ingestion.models import CodexExtensions

        codex = CodexExtensions(
            title=value.title,
            preview=value.preview,
            forked_from_id="shareable" if topology.forked else None,
            spawn_parent_thread_id="shareable" if topology.spawned else None,
            spawn_depth=topology.spawn_depth,
            multi_agent_version=topology.multi_agent_version,
            multi_agent_mode=topology.multi_agent_mode,
            canonical_spawn_origins={
                str(origin.target_session_id): CanonicalSpawnOrigin(
                    event_id=uuid5(
                        _SYNTHETIC_REQUEST_NAMESPACE,
                        f"spawn:{value.session_id}:{origin.target_session_id}",
                    ),
                    turn_id=origin.turn_id,
                    item_id=origin.item_id,
                    tool_name=origin.tool_name,
                )
                for origin in topology.spawn_origins
            },
        )
    elif value.vendor == Vendor.PI:
        from coding_trajectory.ingestion.models import PiExtensions

        pi = PiExtensions(title=value.title)
    if claude is None and codex is None and pi is None:
        return None
    return VendorExtensions(claude_code=claude, codex=codex, pi=pi)


def _to_edge(value: ShareableEdge) -> SessionEdge:
    return SessionEdge(
        type=value.kind,
        source_session_id=value.source_session_id,
        target_session_id=value.target_session_id,
        source_turn_id=value.origin.turn_id,
        source_item_id=value.origin.item_id,
        provenance=value.provenance,
        confidence=value.confidence,
        metadata={"tool_name": value.tool_name} if value.tool_name else None,
    )


def _portable_project(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().replace("\\", "/")
    path = PurePath(normalized)
    if path.is_absolute() or _HOST_PATH.match(value.strip()):
        return _bounded(path.name)
    return _bounded(value)


def _portable_path(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().replace("\\", "/")
    path = PurePath(normalized)
    safe_parts = [part for part in path.parts if part not in {"/", "..", "."}]
    if not safe_parts:
        return None
    if path.is_absolute() or _HOST_PATH.match(value.strip()):
        safe_parts = safe_parts[-1:]
    return _bounded("/".join(safe_parts))


def _bounded(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:512] or None


def _usage_int(value: dict[str, Any], *keys: str) -> int:
    result = _usage_optional_int(value, *keys)
    return result or 0


def _usage_optional_int(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            return item
    return None


def _usage_optional_float(value: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool) and item >= 0:
            result = float(item)
            if math.isfinite(result):
                return result
    return None


def _cost_text(value: float | None) -> str | None:
    if value is None:
        return None
    return "0.0" if value == 0 else repr(value)


def _reject_embedded_content(value: Any, *, field: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if child not in (None, "", [], {}) and any(
                marker in normalized for marker in ("data_uri", "blob", "media")
            ):
                raise ValueError(f"shareable graph retained {key}")
            _reject_embedded_content(child, field=key)
    elif isinstance(value, list):
        for child in value:
            _reject_embedded_content(child, field=field)
    elif isinstance(value, str):
        if len(value) > 512:
            raise ValueError(f"shareable graph retained unbounded string in {field}")
        if value.lstrip().lower().startswith("data:"):
            raise ValueError(f"shareable graph retained data URI in {field}")
        if _HOST_PATH.match(value):
            raise ValueError(f"shareable graph retained a host path in {field}")
        if len(value) >= 128 and _BASE64_BODY.fullmatch(value):
            raise ValueError(f"shareable graph retained a base64-like body in {field}")


__all__ = [
    "MAX_SHAREABLE_ARTIFACT_BYTES",
    "MAX_SHAREABLE_PUBLICATION_BYTES",
    "SHAREABLE_GRAPH_SCHEMA_VERSION",
    "ShareableGraphArtifact",
    "build_shareable_graph_artifact",
    "build_shareable_segments",
    "shareable_session_graph",
]
