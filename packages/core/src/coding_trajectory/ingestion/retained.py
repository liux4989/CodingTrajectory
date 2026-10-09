"""Body-free canonical retention, independent of publication and transport.

Measure transient content before dropping it. Only bounded narrative previews,
credential-redacted tool descriptions and allowlisted operational evidence
survive. This is private workspace retention, not a public-sharing sanitizer.
The legacy ``published_fact_projection`` marker means bodies are unavailable;
keep it until all semantic consumers can distinguish retained from raw items.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit
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
from coding_trajectory.analysis.tool_summary_shared import (
    AGENT_COLLAB,
    EDIT_FILE,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    SESSION_HANDOFF,
    SUBAGENT_TASK,
    TODO_LIST,
    VENDOR_TOOL_CONCEPT,
    WEB_FETCH,
    WEB_SEARCH,
    WRITE_FILE,
)
from coding_trajectory.analysis.tool_summary_shell import classify_verification_command
from coding_trajectory.ingestion.graph import canonical_spawn_origins
from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    event_for_turn_user_request,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    AmpExtensions,
    CanonicalSpawnOrigin,
    ClaudeCodeExtensions,
    CodexExtensions,
    CommandExecutionItem,
    ContextSourceMeasurement,
    ContextUsageObservation,
    Event,
    EventType,
    FileChangeItem,
    Item,
    ItemMeasurements,
    PiExtensions,
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

_REQUEST_NAMESPACE = uuid5(NAMESPACE_URL, "codingtrajectory:chronicle-request")
_STATUS_TOKEN = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")
_HOST_PATH = re.compile(
    r"^(?:~/|/Users/|/home/|/root/|/private/|/tmp/|/var/|/Volumes/|"
    r"/workspace/|/workspaces/|/mnt/|/srv/|/opt/|[A-Za-z]:[\\/])"
)
_CONTEXT_LABELS = {
    "base_system": {"Base instructions", "System prompt & tools"},
    "developer_instructions": {"Developer instructions"},
    "agents_md": {"AGENTS.md"},
    "skills": {"Skills"},
    "mcp": {"Tools / MCP"},
    "memory": {"Memory"},
    "unattributed_context": {"Unattributed context"},
    "retained_user_input": {"Retained requests"},
    "compacted_history": {"Compacted history"},
    "system_tools": {"System tools"},
}
_DETAIL_KINDS = {
    READ_FILE: "file",
    EDIT_FILE: "file",
    WRITE_FILE: "file",
    LIST_FILES: "file",
    SEARCH_TEXT: "search",
    RUN_COMMAND: "command",
    WEB_FETCH: "web",
    WEB_SEARCH: "web",
}
_LIFECYCLES = {
    "completed": "completed",
    "success": "completed",
    "succeeded": "completed",
    "done": "completed",
    "failed": "failed",
    "error": "failed",
    "errored": "failed",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "aborted": "interrupted",
}
_BoundedString = Annotated[str, Field(max_length=512)]


class RetainedOutputEvidence(BaseModel):
    """Validated operational evidence, never a raw tool-output body.

    No current processor may retain a preview or claim complete retention.
    Missing evidence stays missing, including on already-retained items.
    """

    model_config = ConfigDict(extra="forbid")
    processor: _BoundedString
    processor_version: int = Field(default=1, ge=1)
    lifecycle: Literal["completed", "failed", "interrupted", "unknown"]
    outcome: _BoundedString | None = None
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_chars: int = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_method: Literal["provider_reported", "tokenizer_estimate", "not_measured"]
    tokenizer: _BoundedString | None = None
    provider: _BoundedString | None = None
    truncated: bool = False
    original_tokens: int | None = Field(default=None, ge=0)
    facts: dict[_BoundedString, bool | int | _BoundedString] | None = Field(
        default=None, max_length=8
    )
    preview: Annotated[str, Field(min_length=1, max_length=280)] | None = None
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=64)
    retention: Literal["not_applicable", "not_retained", "preview", "complete"]
    searchable: Literal["complete", "preview", "facts_only", "none"]

    @model_validator(mode="after")
    def _no_raw_output(self) -> RetainedOutputEvidence:
        if self.preview is not None or self.retention == "complete":
            raise ValueError("no current evidence processor retains output bodies")
        return self


def output_evidence_from_item(item: Item) -> RetainedOutputEvidence | None:
    """Read retained evidence without importing a publication contract."""
    raw = item.vendor_data.get("chronicle_output_evidence")
    return RetainedOutputEvidence.model_validate(raw) if isinstance(raw, dict) else None


def retain_session_graph(graph: SessionGraph) -> SessionGraph:
    """Directly normalize a transient graph into body-free canonical objects.

    Source event, session, edge and observation order is retained. Turn/item
    sequence ordering matches the former retained graph's canonical ordering.
    Input objects are never mutated or shared with the returned graph.
    """
    index = build_session_graph_index(graph)
    counter = counter_for_session_graph(graph)
    latest = max(
        (
            observation
            for session in graph.sessions
            for observation in session.context_usage
        ),
        key=lambda observation: observation.timestamp,
        default=None,
    )
    with scoped_counter(counter):
        sessions = [
            _retain_session(
                session, index, counter.name, latest.provider if latest else None
            )
            for session in graph.sessions
        ]
    project = graph.project_identifier
    if project:
        path = PurePath(project.strip().replace("\\", "/"))
        project = _bounded(
            path.name
            if path.is_absolute() or _HOST_PATH.match(project.strip())
            else project
        )
    else:
        project = None
    return SessionGraph(
        root_session_id=graph.root_session_id,
        project_identifier=project,
        sessions=sessions,
        edges=[_retain_edge(edge) for edge in graph.edges],
        summary=SessionGraphSummary(
            root_session_id=graph.root_session_id,
            started_at=min((session.started_at for session in sessions), default=None),
            ended_at=max(
                (
                    session.ended_at
                    for session in sessions
                    if session.ended_at is not None
                ),
                default=None,
            ),
            session_count=len(sessions),
            turn_count=sum(len(session.turns) for session in sessions),
            vendors=sorted(
                {session.vendor for session in sessions},
                key=lambda vendor: vendor.value,
            ),
        ),
    )


def _retain_session(
    session: Session, index: SessionGraphIndex, tokenizer: str, provider: str | None
) -> Session:
    # Extract while raw context and response bodies are still available.
    measurements = session.measurements or extract_session_measurements(session)
    sources = []
    history = []
    for source in [*measurements.context_sources, *measurements.compaction_history]:
        known = source.label in _CONTEXT_LABELS.get(source.key, set())
        value = ContextSourceMeasurement(
            timestamp=source.timestamp,
            key=source.key if known else "other_context",
            label=source.label if known else "Other context",
            reported_tokens=source.reported_tokens,
            chars=source.chars,
            tokens=source.tokens,
        )
        (
            history
            if value.key in {"retained_user_input", "compacted_history"}
            else sources
        ).append(value)
    turn_by_event: dict[UUID, UUID] = {}
    for turn in session.turns:
        for event_id in turn.event_ids:
            turn_by_event.setdefault(event_id, turn.turn_id)
        if turn.user_request_event_id is not None:
            turn_by_event.setdefault(turn.user_request_event_id, turn.turn_id)
        for item in turn.items:
            for event_id in item.event_ids:
                turn_by_event.setdefault(event_id, turn.turn_id)
    turn_event_ids: dict[UUID, list[UUID]] = {}
    for event in session.events:
        if (owner := turn_by_event.get(event.event_id)) is not None:
            turn_event_ids.setdefault(owner, []).append(event.event_id)
    requests = {}
    turns = []
    usage = []
    for turn in sorted(session.turns, key=lambda value: value.sequence):
        request = extract_user_request(index, turn, session=session)
        request_id = None
        if request and (preview := _preview(request.get("content"))) is not None:
            request_id = turn.user_request_event_id or uuid5(
                _REQUEST_NAMESPACE, str(turn.turn_id)
            )
            source_event = event_for_turn_user_request(index, turn)
            size = event_text_size(source_event) if source_event is not None else None
            key = (
                "team_request_summary"
                if request.get("source") in {"team_lead", "parent_agent"}
                else "text"
            )
            text = (
                f"<command-name>{preview}</command-name>"
                if request.get("type") == "command"
                else preview
            )
            payload = {
                key: text,
                "preview_truncated": len(request["content"].strip()) > 280
                or bool(source_event and source_event.payload.get("preview_truncated")),
            }
            if size is not None:
                payload[CONTENT_SIZE_MEASUREMENT_KEY] = {
                    "chars": size.chars,
                    "tokens": size.tokens,
                }
            requests[request_id] = payload
        event_ids = set(turn.event_ids)
        observations = [
            observation
            for observation in session.context_usage
            if observation.source_event_id is not None
            and observation.source_event_id in event_ids
        ]
        usage.extend(_retain_usage(observation) for observation in observations)
        ids = list(turn_event_ids.get(turn.turn_id, []))
        if not session.events:
            ids = [observation.source_event_id for observation in observations]
        if request_id is not None and request_id not in ids:
            ids.insert(0, request_id)
        item_ids = {
            call_id: item.item_id
            for item in turn.items
            if isinstance((call_id := getattr(item, "tool_call_id", None)), str)
            and call_id
        }
        completions: dict[str, datetime] = {}
        for item in turn.items:
            call_id = getattr(item, "tool_call_id", None)
            if isinstance(call_id, str) and call_id and item.completed_at is not None:
                completions[call_id] = max(
                    completions.get(call_id, item.completed_at), item.completed_at
                )
        turns.append(
            Turn(
                turn_id=turn.turn_id,
                session_id=session.session_id,
                sequence=turn.sequence,
                started_at=turn.started_at,
                ended_at=turn.ended_at,
                status=turn.status,
                timing_source=turn.timing_source,
                user_request_event_id=request_id,
                event_ids=list(dict.fromkeys(ids)),
                team_state=_retain_team_state(turn.team_state),
                items=[
                    _retain_item(
                        item, session, turn, item_ids, completions, tokenizer, provider
                    )
                    for item in sorted(turn.items, key=lambda value: value.sequence)
                ],
            )
        )
    events = []
    for event in session.events:
        payload = {}
        for key in ("status", "state"):
            value = event.payload.get(key)
            if isinstance(value, str) and _STATUS_TOKEN.fullmatch(value):
                payload["status"] = value
                break
        events.append(
            Event(
                event_id=event.event_id,
                session_id=session.session_id,
                timestamp=event.timestamp,
                type=event.type,
                vendor_source=session.vendor,
                payload=requests.get(event.event_id, payload).copy(),
            )
        )
    if not session.events:
        for turn in turns:
            if turn.user_request_event_id is not None:
                payload = requests[turn.user_request_event_id].copy()
                payload.pop(CONTENT_SIZE_MEASUREMENT_KEY, None)
                events.append(
                    Event(
                        event_id=turn.user_request_event_id,
                        session_id=session.session_id,
                        timestamp=turn.started_at,
                        type=EventType.USER_PROMPT_SUBMITTED,
                        vendor_source=session.vendor,
                        payload=payload,
                    )
                )
    runtime_fields = {
        "timestamp",
        "kind",
        "duration_ms",
        "time_to_first_token_ms",
        "num_turns",
        "pre_tokens",
        "post_tokens",
        "cumulative_dropped_tokens",
        "effort_from",
        "effort_to",
        "comp_hash",
        "runtime_config_hashes",
    }
    return Session(
        session_id=session.session_id,
        vendor=session.vendor,
        model=session.model,
        reasoning_effort=session.reasoning_effort,
        agent_name=session.agent_name,
        cwd=session.cwd,
        started_at=session.started_at,
        ended_at=session.ended_at,
        parent_session_id=session.parent_session_id,
        status=session.status,
        events=events,
        turns=turns,
        context_usage=usage,
        runtime_observations=[
            RuntimeObservation(**entry.model_dump(include=runtime_fields))
            for entry in session.runtime_observations
        ],
        measurements=SessionMeasurements(
            context_sources=sources,
            compaction_history=history,
            llm_response_count=measurements.llm_response_count,
            llm_response_text_sizes=[
                entry.model_copy(deep=True)
                for entry in measurements.llm_response_text_sizes
            ],
        ),
        extensions=_retain_extensions(session),
    )


def _retain_item(
    item: Item,
    session: Session,
    turn: Turn,
    item_ids: dict[str, UUID],
    completions: dict[str, datetime],
    tokenizer: str,
    provider: str | None,
) -> Item:
    measurements = item.measurements or extract_item_measurements(item)
    summary = _tool_summary(item, measurements, session.cwd)
    reconstructed = item.vendor_data.get("published_fact_projection") is True
    retained = item.vendor_data.get("chronicle_semantics")
    retained = retained if isinstance(retained, dict) else {}
    verification = _bounded(retained.get("verification_kind"))
    resolution = _bounded(retained.get("resolution_key"))
    if not reconstructed:
        if isinstance(item, CommandExecutionItem):
            verification = verification or classify_verification_command(item.command)
            command = tool_input_summary(item.command)
            if command:
                resolution = "command:" + hashlib.sha256(command.encode()).hexdigest()
        elif isinstance(item, FileChangeItem) and item.path:
            resolution = f"file:{_portable_path(item.path)}"
        elif summary:
            resolution = f"tool:{summary['name']}"
    semantic = {
        key: value
        for key, raw in (
            ("verification_kind", verification),
            ("resolution_key", resolution),
        )
        if (value := _bounded(raw)) is not None
    }
    path = (
        _portable_path(item.path, session.cwd)
        if isinstance(item, FileChangeItem)
        else None
    )
    evidence = _output_evidence(
        item, measurements, summary, semantic, path, tokenizer, provider
    )
    vendor_data: dict[str, Any] = {
        "chronicle_semantics": semantic,
        "published_fact_projection": True,
    }
    if evidence is not None:
        vendor_data["chronicle_output_evidence"] = evidence.model_dump(
            mode="json", exclude_none=True
        )
    projection = item.vendor_data.get("chronicle_projection")
    projection = projection if isinstance(projection, dict) else {}
    activity = item.vendor_data.get("activity")
    provenance = activity.get("provenance") if isinstance(activity, dict) else None
    provenance = provenance if isinstance(provenance, dict) else {}
    parent = (
        item_ids.get(provenance.get("parent_tool_call_id"))
        if isinstance(provenance.get("parent_tool_call_id"), str)
        else None
    )
    if parent is None:
        try:
            parent = UUID(str(projection["parent_item_id"]))
        except (KeyError, TypeError, ValueError):
            pass
    if parent is not None:
        vendor_data["chronicle_projection"] = {"parent_item_id": str(parent)}
        nested = provenance.get("nested_index", projection.get("nested_index"))
        if isinstance(nested, int) and not isinstance(nested, bool):
            vendor_data["chronicle_projection"]["nested_index"] = nested
    common = {
        "item_id": item.item_id,
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "sequence": item.sequence,
        "started_at": item.started_at,
        "completed_at": item.completed_at
        or completions.get(getattr(item, "tool_call_id", None)),
        "status": str(getattr(item.status, "value", item.status))
        if item.status is not None
        else None,
        "event_ids": list(item.event_ids),
        "vendor_data": vendor_data,
        "measurements": ItemMeasurements(
            **measurements.model_dump(
                exclude={"input_summary", "text_preview", "tool_summary"}
            ),
            text_preview=_preview(measurements.text_preview),
            tool_summary=summary,
        ),
    }
    name = _bounded(getattr(item, "tool_name", None)) or (
        summary["name"] if summary else None
    )
    if isinstance(item, AgentMessageItem):
        return AgentMessageItem(**common)
    if isinstance(item, ReasoningItem):
        return ReasoningItem(**common)
    if isinstance(item, CommandExecutionItem):
        return CommandExecutionItem(**common, tool_name=name, exit_code=item.exit_code)
    if isinstance(item, FileChangeItem):
        return FileChangeItem(
            **common, tool_name=name, path=path, operation=_bounded(item.operation)
        )
    if isinstance(item, PlanItem):
        return PlanItem(**common, tool_name=name)
    return ToolCallItem(**common, tool_name=name)


def _tool_summary(
    item: Item, measurements: ItemMeasurements, cwd: str | None
) -> dict[str, Any] | None:
    if (
        measurements.tool_summary is None
        and item.vendor_data.get("published_fact_projection") is True
    ):
        return None
    raw = measurements.tool_summary or summarize_tool_call(item)
    if not isinstance(raw, dict) or not (name := _bounded(raw.get("name"))):
        return None
    summary: dict[str, Any] = {"name": name}
    for key in (
        "status",
        "optimization_profile",
        "activity_kind",
        "activity_source",
        "activity_outcome",
        "activity_fidelity",
        "activity_wrapper_status",
    ):
        if (value := _bounded(raw.get(key))) is not None:
            summary[key] = value
    if isinstance(raw.get("activity_hidden"), bool):
        summary["activity_hidden"] = raw["activity_hidden"]
    description = raw.get("description")
    if isinstance(description, str) and (
        target := _safe_detail_target(description, cwd=cwd)
    ):
        kind = (
            "coordination"
            if name in {TODO_LIST, SUBAGENT_TASK, AGENT_COLLAB, SESSION_HANDOFF}
            else _DETAIL_KINDS.get(name, "tool")
        )
        truncated = len(" ".join(description.split())) > 280 or bool(
            raw.get("description_truncated")
        )
        summary.update(
            detail={
                "kind": kind,
                "target": target,
                "scope": None,
                "truncated": truncated,
                "safety": "sanitized",
            },
            description=target,
            description_truncated=truncated,
        )
    return summary


def _output_evidence(
    item: Item,
    measurements: ItemMeasurements,
    summary: dict[str, Any] | None,
    semantic: dict[str, str],
    path: str | None,
    tokenizer: str,
    provider: str | None,
) -> RetainedOutputEvidence | None:
    if item.vendor_data.get("published_fact_projection") is True:
        return output_evidence_from_item(item)
    tool_name = getattr(item, "tool_name", None)
    if item.kind not in {"tool_call", "command_execution", "file_change", "plan"} or (
        item.kind == "plan" and not tool_name
    ):
        return None
    facts: dict[str, bool | int | str] = {}
    if isinstance(item, CommandExecutionItem):
        processor = "ct.output_evidence.command.v1"
        if semantic.get("verification_kind"):
            facts["verification_kind"] = semantic["verification_kind"]
        if item.exit_code is not None:
            facts["exited_zero"] = item.exit_code == 0
    elif isinstance(item, FileChangeItem):
        processor = "ct.output_evidence.file_change.v1"
        if item.operation:
            facts["operation"] = _bounded(item.operation) or "unknown"
        if path:
            facts["path"] = path
    elif (
        isinstance(tool_name, str)
        and (concept := VENDOR_TOOL_CONCEPT.get(tool_name)) is not None
    ):
        processor = "ct.output_evidence.tool.v1"
        facts["concept"] = _bounded(concept) or "tool"
    else:
        processor = "ct.output_evidence.unknown.v1"
    status = (
        str(getattr(item.status, "value", item.status)).casefold()
        if item.status is not None
        else None
    )
    measured = measurements.output_chars > 0 or measurements.output_truncated
    duration = (
        max(0, round((item.completed_at - item.started_at).total_seconds() * 1000))
        if item.completed_at is not None
        else None
    )
    return RetainedOutputEvidence(
        processor=processor,
        lifecycle=_LIFECYCLES.get(status or "", "unknown"),
        outcome=_bounded((summary or {}).get("activity_outcome") or status),
        exit_code=getattr(item, "exit_code", None),
        duration_ms=duration,
        output_chars=measurements.output_chars,
        output_tokens=measurements.output_tokens if measured else None,
        token_method="tokenizer_estimate" if measured else "not_measured",
        tokenizer=tokenizer if measured else None,
        provider=provider if measured else None,
        truncated=measurements.output_truncated,
        original_tokens=measurements.output_original_tokens,
        facts=facts or None,
        source_event_ids=list(item.event_ids)[:64],
        retention="not_retained" if measured else "not_applicable",
        searchable="facts_only" if facts else "none",
    )


def _retain_extensions(session: Session) -> VendorExtensions:
    extensions = session.extensions
    codex = extensions.codex if extensions else None
    claude = extensions.claude_code if extensions else None
    amp = extensions.amp if extensions else None
    title = next(
        (
            getattr(value, "title", None)
            for value in (codex, claude, extensions.pi if extensions else None, amp)
            if value and getattr(value, "title", None)
        ),
        "",
    )
    title = _safe_detail_target(title, cwd=session.cwd)
    existing = (
        amp.canonical_spawn_origins
        if amp
        else codex.canonical_spawn_origins
        if codex
        else {}
    )
    origins = {
        str(UUID(child)): CanonicalSpawnOrigin(
            event_id=uuid5(
                _REQUEST_NAMESPACE, f"spawn:{session.session_id}:{UUID(child)}"
            ),
            turn_id=origin.turn_id,
            item_id=origin.item_id,
            tool_name=_bounded(origin.tool_name),
        )
        for child, origin in sorted(
            {**existing, **canonical_spawn_origins(session)}.items()
        )
    }
    depth = (
        codex.spawn_depth
        if codex and codex.spawn_depth is not None
        else claude.spawn_depth
        if claude
        else None
    )
    if session.vendor == Vendor.AMP:
        return VendorExtensions(
            amp=AmpExtensions(
                thread_id=f"T-{session.session_id}",
                title=title,
                canonical_spawn_origins=origins,
                spawn_links={
                    child: str(origin.item_id)
                    for child, origin in origins.items()
                    if origin.item_id is not None
                },
            )
        )
    if session.vendor == Vendor.CODEX_CLI:
        return VendorExtensions(
            codex=CodexExtensions(
                title=title,
                preview=_safe_detail_target(codex.preview or "", cwd=session.cwd)
                if codex
                else None,
                agent_path=codex.agent_path if codex else None,
                forked_from_id="chronicle" if codex and codex.forked_from_id else None,
                spawn_parent_thread_id="chronicle"
                if codex and codex.spawn_parent_thread_id
                else None,
                spawn_depth=depth,
                multi_agent_version=_bounded(codex.multi_agent_version)
                if codex
                else None,
                multi_agent_mode=_bounded(codex.multi_agent_mode) if codex else None,
                canonical_spawn_origins=origins,
            )
        )
    if session.vendor == Vendor.CLAUDE_CODE:
        return VendorExtensions(
            claude_code=ClaudeCodeExtensions(
                title=title,
                is_sidechain=bool(claude and claude.is_sidechain),
                spawn_depth=depth,
            )
        )
    return VendorExtensions(pi=PiExtensions(title=title))


def _retain_team_state(state: TeamTurnState | None) -> TeamTurnState | None:
    if state is None or not (state.members or state.tasks):
        return None
    return TeamTurnState(
        members=[
            TeamMemberState(
                member_id=_bounded(member.member_id) or "member",
                session_id=member.session_id,
                agent_type=_bounded(member.agent_type),
            )
            for member in state.members
        ],
        tasks=[
            TeamTaskState(
                task_id=_bounded(task.task_id) or "task",
                status=_bounded(task.status),
                member_id=_bounded(task.member_id),
                blocked_by=[_bounded(value) or "task" for value in task.blocked_by],
            )
            for task in state.tasks
        ],
    )


def _retain_usage(observation: ContextUsageObservation) -> ContextUsageObservation:
    return observation.model_copy(
        deep=True,
        update={
            "usage": _usage(observation.usage),
            "cumulative_usage": _usage(observation.cumulative_usage)
            if observation.cumulative_usage is not None
            else None,
        },
    )


def _usage(raw: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "uncached_input_tokens",
    ):
        camel = key.split("_")[0] + "".join(word.title() for word in key.split("_")[1:])
        value = next(
            (
                raw[name]
                for name in (key, camel)
                if isinstance(raw.get(name), int)
                and not isinstance(raw[name], bool)
                and raw[name] >= 0
            ),
            None,
        )
        if value is not None or key != "uncached_input_tokens":
            result[key] = value or 0
    for key in ("cost_usd", "costUsd"):
        value = raw.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            and math.isfinite(float(value))
        ):
            result["cost_usd"] = float(value)
            break
    return result


def _retain_edge(edge: SessionEdge) -> SessionEdge:
    name = (
        _bounded(edge.metadata.get("tool_name"))
        if isinstance(edge.metadata, dict)
        else None
    )
    return SessionEdge(
        **edge.model_dump(exclude={"metadata"}),
        metadata={"tool_name": name} if name else None,
    )


def _bounded(value: Any) -> str | None:
    return (
        (" ".join(value.split()).strip()[:512] or None)
        if isinstance(value, str)
        else None
    )


def _preview(value: Any) -> str | None:
    return (value[:280].strip() or None) if isinstance(value, str) else None


def _portable_path(value: str | None, cwd: str | None = None) -> str | None:
    if not value:
        return None
    normalized = value.strip().replace("\\", "/")
    if cwd:
        base = cwd.rstrip("/").replace("\\", "/")
        if normalized == base:
            return "."
        if normalized.startswith(base + "/"):
            normalized = normalized[len(base) + 1 :]
    path = PurePath(normalized)
    parts = [part for part in path.parts if part not in {"/", "..", "."}]
    if path.is_absolute() or _HOST_PATH.match(value.strip()):
        parts = parts[-1:]
    return _bounded("/".join(parts))


def _safe_detail_target(value: str, *, cwd: str | None) -> str | None:
    normalized = " ".join(value.split()).strip()
    normalized = re.sub(
        r"https?://[^\s'\"]+", lambda match: _safe_url(match.group(0)), normalized
    )
    normalized = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [redacted]", normalized
    )
    normalized = re.sub(
        r"(?i)((?:password|passwd|token|secret|api[-_]?key|authorization|cookie)"
        r"\s*[:=]\s*|--(?:password|passwd|token|secret|api[-_]?key|authorization|cookie)\s+)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[redacted]",
        normalized,
    )
    return _preview(normalized)


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "[url]"
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


__all__ = [
    "RetainedOutputEvidence",
    "output_evidence_from_item",
    "retain_session_graph",
]
