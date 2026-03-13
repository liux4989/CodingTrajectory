"""Trajectory assembly for replay-first multi-session and multi-agent views."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.ingestion.decorators import ClaudeCodeDecorator
from coding_trajectory.ingestion.models import (
    Event,
    EventConfidence,
    EventProvenance,
    EventType,
    InferenceNote,
    Session,
    Trajectory,
    TrajectoryEdge,
    TrajectoryOperation,
    TrajectorySection,
    TrajectorySessionRef,
    TrajectorySummary,
    Vendor,
)


def decorate_sessions(sessions: list[Session]) -> list[Session]:
    registry = {session.session_id: session for session in sessions}
    decorators = [ClaudeCodeDecorator()]
    updated: list[Session] = []

    for session in sorted(sessions, key=lambda item: (item.started_at, str(item.session_id))):
        enriched = session
        for decorator in decorators:
            enriched = decorator.apply(enriched, registry)
        registry[enriched.session_id] = enriched
        updated.append(enriched)

    return updated


def assemble_project_trajectories(project_identifier: str, sessions: list[Session]) -> list[Trajectory]:
    sessions = decorate_sessions(sessions)
    groups: dict[str, list[Session]] = defaultdict(list)

    for session in sorted(sessions, key=lambda item: (item.started_at, str(item.session_id))):
        groups[_trajectory_group_key(session)].append(session)

    trajectories: list[Trajectory] = []
    for group_key, group_sessions in sorted(groups.items()):
        key = _normalize_project_key(project_identifier)
        trajectory_id = uuid5(NAMESPACE_URL, f"coding-trajectory:{key}:{group_key}")
        session_ids = {session.session_id for session in group_sessions}
        normalized_sessions = [
            session.model_copy(update={"trajectory_id": trajectory_id})
            for session in sorted(group_sessions, key=lambda item: (item.started_at, str(item.session_id)))
        ]
        trajectories.append(
            build_trajectory(
                trajectory_id=trajectory_id,
                project_identifier=key,
                sessions=normalized_sessions,
                extra_session_ids=session_ids,
            )
        )

    return trajectories


def build_trajectory(
    *,
    trajectory_id: UUID,
    project_identifier: str,
    sessions: list[Session],
    extra_session_ids: set[UUID] | None = None,
) -> Trajectory:
    summary = build_trajectory_summary(sessions)
    session_refs = build_session_refs(sessions)
    edges = build_edges(sessions)
    operations = build_operations(trajectory_id, sessions)
    sections = build_sections(trajectory_id, sessions, operations)
    inference_notes = build_inference_notes(sessions, edges)
    multi_agent_mode = detect_multi_agent_mode(sessions, edges, operations)
    task_reference = _task_reference(sessions)

    if extra_session_ids:
        referenced = {session.session_id for session in sessions}
        for session_id in extra_session_ids:
            if session_id not in referenced:
                inference_notes.append(
                    InferenceNote(
                        subject=str(session_id),
                        message="Session id was referenced during grouping but is not present in the assembled trajectory.",
                    )
                )

    return Trajectory(
        trajectory_id=trajectory_id,
        project_identifier=project_identifier,
        task_reference=task_reference,
        multi_agent_mode=multi_agent_mode,
        summary=summary,
        session_refs=session_refs,
        edges=edges,
        operations=operations,
        sections=sections,
        inference_notes=inference_notes,
        sessions=sessions,
    )


def build_trajectory_summary(sessions: list[Session]) -> TrajectorySummary:
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = _max_datetime(session.ended_at for session in sessions)
    root_session = _root_session(sessions)
    vendors = sorted({session.vendor for session in sessions}, key=lambda item: item.value)
    agent_count = len(
        {
            agent_name
            for agent_name in (_agent_name(session) for session in sessions)
            if agent_name
        }
    )

    return TrajectorySummary(
        root_session_id=root_session.session_id if root_session else None,
        started_at=started_at,
        ended_at=ended_at,
        session_count=len(sessions),
        turn_count=sum(len(session.turns) for session in sessions),
        event_count=sum(len(session.events) for session in sessions),
        agent_count=agent_count or None,
        vendors=vendors,
    )


def build_session_refs(sessions: list[Session]) -> list[TrajectorySessionRef]:
    refs: list[TrajectorySessionRef] = []

    for session in sessions:
        refs.append(
            TrajectorySessionRef(
                session_id=session.session_id,
                parent_session_id=session.parent_session_id,
                vendor=session.vendor,
                role=_session_role(session),
                agent_name=_agent_name(session),
                team_name=_team_name(session),
                is_sidechain=_is_sidechain(session),
                collaboration_mode=_collaboration_mode(session),
                started_at=session.started_at,
                ended_at=session.ended_at,
            )
        )

    return refs


def build_edges(sessions: list[Session]) -> list[TrajectoryEdge]:
    edges: list[TrajectoryEdge] = []
    session_map = {session.session_id: session for session in sessions}

    for session in sessions:
        if session.parent_session_id is None:
            continue
        parent = session_map.get(session.parent_session_id)
        edge_type, evidence_ids = _classify_edge(session, parent)
        metadata = {"team_name": _team_name(session)} if _team_name(session) else None
        edges.append(
            TrajectoryEdge(
                type=edge_type,
                source_session_id=session.session_id,
                target_session_id=session.parent_session_id,
                evidence_event_ids=evidence_ids,
                provenance=EventProvenance.DERIVED,
                confidence=EventConfidence.HIGH if evidence_ids else EventConfidence.MEDIUM,
                metadata=metadata,
            )
        )

    return edges


def _classify_edge(child: Session, parent: Session | None) -> tuple[str, list]:
    """Return (edge_type, evidence_event_ids) for a child->parent relationship."""
    if parent is not None:
        for event in parent.events:
            tool_name = event.payload.get("tool_name")
            if event.type == EventType.TOOL_CALL_REQUESTED and tool_name in {"TaskCreate", "TeamCreate"}:
                return "spawned_subagent", [event.event_id]
            if event.type == EventType.SUBTASK_STARTED:
                return "spawned_subagent", [event.event_id]
    return "sidechain_of", []


def build_operations(trajectory_id: UUID, sessions: list[Session]) -> list[TrajectoryOperation]:
    operations: list[TrajectoryOperation] = []
    parent_ids = {session.parent_session_id for session in sessions if session.parent_session_id is not None}

    for session in sessions:
        for event in session.events:
            operation_kind = _event_operation_kind(event)
            if operation_kind is None:
                continue

            scope = "session_graph" if _is_session_graph_operation(session, operation_kind, parent_ids) else "session_span"
            session_ids = [session.session_id]
            if scope == "session_graph" and session.parent_session_id is not None:
                session_ids.append(session.parent_session_id)

            turn_ids = [event.turn_id] if event.turn_id else []
            operation_id = uuid5(
                NAMESPACE_URL,
                json.dumps(
                    {
                        "trajectory_id": str(trajectory_id),
                        "kind": operation_kind,
                        "scope": scope,
                        "session_ids": [str(item) for item in session_ids],
                        "event_id": str(event.event_id),
                    },
                    sort_keys=True,
                ),
            )
            operations.append(
                TrajectoryOperation(
                    operation_id=operation_id,
                    kind=operation_kind,
                    scope=scope,
                    status=_operation_status(event),
                    session_ids=session_ids,
                    event_ids=[event.event_id],
                    turn_ids=turn_ids,
                    start_event_id=event.event_id,
                    end_event_id=event.event_id if _operation_status(event) == "completed" else None,
                    started_at=event.timestamp,
                    ended_at=event.timestamp if _operation_status(event) == "completed" else None,
                    provenance=event.provenance,
                    confidence=event.confidence,
                    metadata=_operation_metadata(event),
                )
            )

    return sorted(operations, key=lambda item: (item.started_at or datetime.min, str(item.operation_id)))


def build_sections(
    trajectory_id: UUID,
    sessions: list[Session],
    operations: list[TrajectoryOperation],
) -> list[TrajectorySection]:
    sections: list[TrajectorySection] = []
    all_session_ids = [session.session_id for session in sessions]
    all_turn_ids = [turn.turn_id for session in sessions for turn in session.turns]
    all_event_ids = [event.event_id for session in sessions for event in session.events]
    started_at = min((session.started_at for session in sessions), default=None)
    ended_at = _max_datetime(session.ended_at for session in sessions)

    sections.append(
        TrajectorySection(
            section_id=uuid5(NAMESPACE_URL, f"{trajectory_id}:main"),
            kind="main",
            title="Main trajectory",
            scope="session_graph" if len(sessions) > 1 else "session_span",
            session_ids=all_session_ids,
            event_ids=all_event_ids,
            turn_ids=all_turn_ids,
            started_at=started_at,
            ended_at=ended_at,
        )
    )

    for operation in operations:
        sections.append(
            TrajectorySection(
                section_id=uuid5(NAMESPACE_URL, f"{trajectory_id}:section:{operation.operation_id}"),
                kind=operation.kind,
                title=_section_title(operation),
                scope=operation.scope,
                operation_id=operation.operation_id,
                session_ids=operation.session_ids,
                event_ids=operation.event_ids,
                turn_ids=operation.turn_ids,
                started_at=operation.started_at,
                ended_at=operation.ended_at,
            )
        )

    return sections


def build_inference_notes(sessions: list[Session], edges: list[TrajectoryEdge]) -> list[InferenceNote]:
    notes: list[InferenceNote] = []

    if any(session.vendor == Vendor.CODEX_CLI for session in sessions):
        notes.append(
            InferenceNote(
                subject="multi_agent_mode",
                message="Codex sessions default to in-session orchestration unless stronger cross-session evidence is present.",
            )
        )

    for session in sessions:
        if _is_sidechain(session) and session.parent_session_id is None:
            notes.append(
                InferenceNote(
                    subject=str(session.session_id),
                    message="Claude sidechain session could not be linked to a parent session with high confidence.",
                )
            )

    if edges:
        notes.append(
            InferenceNote(
                subject="edges",
                message="Session graph edges are derived from session metadata and decorator-enriched linkage, not emitted directly by the vendor.",
            )
        )

    return notes


def detect_multi_agent_mode(
    sessions: list[Session],
    edges: list[TrajectoryEdge],
    operations: list[TrajectoryOperation],
) -> str | None:
    has_session_graph = bool(edges) or any(_is_sidechain(session) for session in sessions)
    has_session_span = any(operation.scope == "session_span" for operation in operations) or any(
        session.vendor == Vendor.CODEX_CLI for session in sessions
    )

    if has_session_graph and has_session_span:
        return "hybrid"
    if has_session_graph:
        return "cross_session"
    if has_session_span:
        return "in_session"
    return None


def _trajectory_group_key(session: Session) -> str:
    parent = session.parent_session_id
    if parent is not None:
        return f"session:{parent}"

    team_name = _team_name(session)
    if session.vendor == Vendor.CLAUDE_CODE and team_name:
        return f"claude-team:{team_name}"

    return f"session:{session.session_id}"


def _normalize_project_key(value: str) -> str:
    import re

    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()


def _root_session(sessions: list[Session]) -> Session | None:
    for session in sessions:
        if session.parent_session_id is None and not _is_sidechain(session):
            return session
    return min(sessions, key=lambda item: (item.started_at, str(item.session_id)), default=None)


def _event_operation_kind(event: Event) -> str | None:
    payload = event.payload
    tool_name = payload.get("tool_name")

    if event.type == EventType.SUBTASK_STARTED:
        return "teammate" if payload.get("team_name") or tool_name == "TeamCreate" else "subagent"
    if event.type == EventType.CONTEXT_COMPACTION_STARTED or event.type == EventType.CONTEXT_COMPACTION_COMPLETED:
        return "compact"
    if event.type == EventType.SESSION_RESUMED:
        return "resume"
    if event.type == EventType.TASK_COMPLETED and event.vendor_source == Vendor.CODEX_CLI:
        return "subagent"
    if event.type == EventType.TOOL_CALL_REQUESTED:
        if tool_name == "spawn_agent":
            return "subagent"
        if tool_name == "send_input":
            return "handoff"
        if tool_name == "resume_agent":
            return "resume"
    return None


def _operation_status(event: Event) -> str:
    if event.type == EventType.CONTEXT_COMPACTION_COMPLETED:
        return "completed"
    if event.type == EventType.SUBTASK_COMPLETED:
        return "completed"
    if event.type == EventType.TASK_COMPLETED:
        return "completed"
    return "started"


def _operation_metadata(event: Event) -> dict[str, Any] | None:
    metadata_keys = (
        "tool_name",
        "tool_call_id",
        "task_id",
        "task_content",
        "task_status",
        "team_name",
        "lead_agent_id",
        "trigger",
        "pre_tokens",
        "messages_summarized",
    )
    metadata = {key: event.payload[key] for key in metadata_keys if key in event.payload}
    return metadata or None


def _is_session_graph_operation(session: Session, operation_kind: str, parent_ids: set | None = None) -> bool:
    if operation_kind in {"subagent", "teammate"}:
        if _is_sidechain(session) or session.parent_session_id is not None:
            return True
        if parent_ids and session.session_id in parent_ids:
            return True
    return False


def _session_role(session: Session) -> str:
    if _is_sidechain(session):
        return "subagent"
    return "primary"


def _agent_name(session: Session) -> str | None:
    extensions = session.extensions
    if extensions is None:
        return None
    if extensions.claude_code and extensions.claude_code.agent_name:
        return extensions.claude_code.agent_name
    if extensions.codex and extensions.codex.agent_nickname:
        return extensions.codex.agent_nickname
    return None


def _team_name(session: Session) -> str | None:
    extensions = session.extensions
    if extensions and extensions.claude_code:
        return extensions.claude_code.team_name
    return None


def _is_sidechain(session: Session) -> bool:
    extensions = session.extensions
    return bool(extensions and extensions.claude_code and extensions.claude_code.is_sidechain)


def _collaboration_mode(session: Session) -> str | None:
    extensions = session.extensions
    if extensions and extensions.codex:
        return extensions.codex.collaboration_mode
    return None


def _section_title(operation: TrajectoryOperation) -> str:
    if operation.kind == "compact":
        return "Context compaction"
    if operation.kind == "resume":
        return "Resume"
    if operation.kind == "handoff":
        return "Handoff"
    if operation.kind == "teammate":
        return "Teammate"
    return "Subagent"


def _task_reference(sessions: list[Session]) -> str | None:
    for session in sessions:
        for turn in session.turns:
            if turn.user_request:
                return turn.user_request
    return None


def _max_datetime(values: Any) -> datetime | None:
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None
