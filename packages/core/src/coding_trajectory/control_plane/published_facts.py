"""Typed bounded publication facts and direct canonical reconstruction.

Local and remote historical APIs consume the same typed fact rows. Publication
alone wraps them in a bounded ``PublishedFactSet``. Facts never contain raw tool
input/output, command
stdout/stderr, patch or file bodies, full prompts/transcripts/reasoning, raw
event payloads, vendor_data blobs, or media. Bounded semantic descriptions may
include command arguments and tool target paths. Explicit credential redaction
is best-effort; these internal-workspace facts are not public-sharing exports.

Row hashes and the fact-set digest use canonical JSON spelling that the
Cloudflare authority recomputes with its sorted-key ``stable()`` encoder. Fact
payloads therefore contain no floats:
non-integer numbers are normalized to decimal strings at derivation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coding_trajectory.control_plane.fact_constants import (
    FACT_SET_SCHEMA_VERSION,
    MAX_FACT_ROWS_PER_GRAPH,
)
from coding_trajectory.control_plane.fact_projection import (
    ChronicleCoverage,
    ChronicleEdge,
    ChronicleEvent,
    ChronicleGraphSummary,
    ChronicleItem,
    ChronicleItemMeasurements,
    ChronicleItemSemantic,
    ChronicleRequestUsage,
    ChronicleRuntimeObservation,
    ChronicleSession,
    ChronicleSessionMeasurements,
    ChronicleSessionTopology,
    ChronicleTeamState,
    ChronicleToolOutputEvidence,
    ChronicleTurn,
    ChronicleUsage,
    ChronicleUserRequest,
    _reject_embedded_content,
    _to_edge,
    _to_session,
)
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.models import SessionGraph, SessionGraphSummary, Vendor

_CostText = Annotated[
    str,
    Field(
        max_length=64,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$",
    ),
]

# The measured legitimate maximum is 10,602,862 bytes. 16 MiB provides 58%
# headroom while keeping graph-at-a-time validation bounded.
MAX_FACT_SET_BYTES = 16 * 1024 * 1024
MAX_FACT_ROW_BYTES = 512 * 1024
MAX_FACT_READ_PAGE_BYTES = 1024 * 1024
MAX_FACT_SESSIONS = 512
MAX_FACT_TURNS = 32_768
MAX_FACT_ITEMS = 131_072
MAX_FACT_EVENTS = 131_072
MAX_FACT_EDGES = 8_192
MAX_FACT_REQUESTS = 65_536
MAX_FACT_RUNTIME = 32_768
MAX_FACT_MODELS = 256

_FACT_NAMESPACE = uuid5(NAMESPACE_URL, "codingtrajectory:published-fact")

FACT_KIND_LIMITS: dict[str, int] = {
    "graph": 1,
    "session": MAX_FACT_SESSIONS,
    "turn": MAX_FACT_TURNS,
    "item": MAX_FACT_ITEMS,
    "event": MAX_FACT_EVENTS,
    "edge": MAX_FACT_EDGES,
    "request": MAX_FACT_REQUESTS,
    "model": MAX_FACT_MODELS,
    "runtime": MAX_FACT_RUNTIME,
    "measurement": MAX_FACT_SESSIONS,
    "output_evidence": MAX_FACT_ITEMS,
}

DERIVED_FACT_KINDS = tuple(FACT_KIND_LIMITS)


class FactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphFactPayload(FactModel):
    summary: ChronicleGraphSummary
    coverage: ChronicleCoverage


class SessionFactPayload(FactModel):
    session_id: UUID
    parent_session_id: UUID | None = None
    vendor: Vendor
    started_at: datetime
    ended_at: datetime | None = None
    status: str = Field(max_length=512)
    model: str | None = Field(default=None, max_length=512)
    reasoning_effort: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=280)
    preview: str | None = Field(default=None, min_length=1, max_length=280)
    agent_name: str | None = Field(default=None, max_length=512)
    cwd: str | None = None
    agent_path: str | None = None
    topology: ChronicleSessionTopology = Field(default_factory=ChronicleSessionTopology)


class TurnFactPayload(FactModel):
    turn_id: UUID
    sequence: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime | None = None
    status: str = Field(max_length=512)
    user_request: ChronicleUserRequest | None = None
    team_state: ChronicleTeamState | None = None


class ItemFactPayload(FactModel):
    item_id: UUID
    event_ids: list[UUID] = Field(max_length=64)
    sequence: int = Field(ge=0)
    kind: Literal[
        "agent_message",
        "tool_call",
        "command_execution",
        "file_change",
        "reasoning",
        "plan",
    ]
    started_at: datetime
    completed_at: datetime | None = None
    status: str | None = Field(default=None, max_length=512)
    tool_name: str | None = Field(default=None, max_length=512)
    operation: str | None = Field(default=None, max_length=512)
    exit_code: int | None = None
    path: str | None = Field(default=None, max_length=512)
    projection_parent_item_id: UUID | None = None
    nested_index: int | None = Field(default=None, ge=0)
    measurements: ChronicleItemMeasurements = Field(
        default_factory=ChronicleItemMeasurements
    )
    semantic: ChronicleItemSemantic = Field(default_factory=ChronicleItemSemantic)


class FactRowBase(FactModel):
    graph_id: UUID
    fact_id: UUID
    parent_id: UUID | None = None
    order_index: int | None = Field(default=None, ge=0)
    row_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def hashable_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, exclude={"row_hash"})


class GraphFactRow(FactRowBase):
    kind: Literal["graph"]
    payload: GraphFactPayload


class SessionFactRow(FactRowBase):
    kind: Literal["session"]
    payload: SessionFactPayload


class TurnFactRow(FactRowBase):
    kind: Literal["turn"]
    payload: TurnFactPayload


class ItemFactRow(FactRowBase):
    kind: Literal["item"]
    payload: ItemFactPayload


class EventFactRow(FactRowBase):
    kind: Literal["event"]
    payload: ChronicleEvent


class EdgeFactRow(FactRowBase):
    kind: Literal["edge"]
    payload: ChronicleEdge


class RequestFactRow(FactRowBase):
    kind: Literal["request"]
    payload: ChronicleRequestUsage


class ModelFactPayload(FactModel):
    model: str | None = Field(default=None, max_length=512)
    providers: list[str] = Field(default_factory=list, max_length=16)
    request_count: int = Field(ge=0)
    usage: ChronicleUsage


class RuntimeFactRow(FactRowBase):
    kind: Literal["runtime"]
    payload: ChronicleRuntimeObservation


class MeasurementFactRow(FactRowBase):
    kind: Literal["measurement"]
    payload: ChronicleSessionMeasurements


class OutputEvidenceFactRow(FactRowBase):
    kind: Literal["output_evidence"]
    payload: ChronicleToolOutputEvidence


class ModelFactRow(FactRowBase):
    kind: Literal["model"]
    payload: ModelFactPayload


FactRow = Annotated[
    GraphFactRow
    | SessionFactRow
    | TurnFactRow
    | ItemFactRow
    | EventFactRow
    | EdgeFactRow
    | RequestFactRow
    | ModelFactRow
    | RuntimeFactRow
    | MeasurementFactRow
    | OutputEvidenceFactRow,
    Field(discriminator="kind"),
]


def compute_row_hash(view: dict[str, Any]) -> str:
    """Hash one fact row's canonical identity+payload spelling."""

    return hashlib.sha256(canonical_json(view).encode()).hexdigest()


def compute_fact_set_digest(graph_id: UUID, rows: list[FactRowBase]) -> str:
    basis = {
        "schema_version": FACT_SET_SCHEMA_VERSION,
        "graph_id": str(graph_id),
        "rows": [
            [row.kind, str(row.fact_id), row.row_hash]
            for row in sorted(rows, key=lambda row: (row.kind, str(row.fact_id)))
        ],
    }
    return hashlib.sha256(canonical_json(basis).encode()).hexdigest()


def _validate_fact_relationships(graph_id: UUID, rows: list[FactRowBase]) -> None:
    by_kind = {
        kind: {row.fact_id: row for row in rows if row.kind == kind}
        for kind in DERIVED_FACT_KINDS
    }
    graph_rows = list(by_kind["graph"].values())
    if len(graph_rows) != 1:
        raise ValueError("fact set requires exactly one graph row")
    graph_row = graph_rows[0]
    if (
        graph_row.fact_id != graph_id
        or graph_row.parent_id is not None
        or graph_row.payload.summary.root_session_id != graph_id
    ):
        raise ValueError("graph fact identity mismatch")

    sessions = by_kind["session"]
    turns = by_kind["turn"]
    items = by_kind["item"]
    events = by_kind["event"]
    if graph_id not in sessions:
        raise ValueError("graph root session is not retained")

    def validate_sequences(
        grouped: dict[UUID, list[FactRowBase]], *, label: str
    ) -> None:
        for grouped_rows in grouped.values():
            sequences = [row.payload.sequence for row in grouped_rows]
            if len(sequences) != len(set(sequences)) or any(
                row.order_index != row.payload.sequence for row in grouped_rows
            ):
                raise ValueError(f"{label} ordering is invalid")

    for row in sessions.values():
        if row.fact_id != row.payload.session_id or row.parent_id != graph_id:
            raise ValueError("session fact identity mismatch")
        parent_session_id = row.payload.parent_session_id
        if parent_session_id is not None and parent_session_id not in sessions:
            raise ValueError("session parent reference is not retained")
        for origin in row.payload.topology.spawn_origins:
            if origin.turn_id is not None:
                turn = turns.get(origin.turn_id)
                if turn is None or turn.parent_id != row.fact_id:
                    raise ValueError("session spawn turn ownership mismatch")
            if origin.item_id is not None:
                item = items.get(origin.item_id)
                if item is None or item.parent_id != origin.turn_id:
                    raise ValueError("session spawn item ownership mismatch")

    for row in turns.values():
        if row.fact_id != row.payload.turn_id or row.parent_id not in sessions:
            raise ValueError("turn fact identity mismatch")
    turns_by_session: dict[UUID, list[FactRowBase]] = {}
    for row in turns.values():
        turns_by_session.setdefault(row.parent_id, []).append(row)
    validate_sequences(turns_by_session, label="turn")

    for row in items.values():
        if row.fact_id != row.payload.item_id or row.parent_id not in turns:
            raise ValueError("item fact identity mismatch")
        projection_parent_id = row.payload.projection_parent_item_id
        if projection_parent_id is None:
            if row.payload.nested_index is not None:
                raise ValueError("item nested index has no projection parent")
        else:
            parent = items.get(projection_parent_id)
            if (
                parent is None
                or parent.fact_id == row.fact_id
                or parent.parent_id != row.parent_id
            ):
                raise ValueError("item projection parent ownership mismatch")
            if parent.payload.measurements.projection_only:
                raise ValueError("item projection parent is not canonical")
            if not row.payload.measurements.projection_only:
                raise ValueError("item projection child owns canonical content")
        for event_id in row.payload.event_ids:
            event = events.get(event_id)
            if event is None or event.payload.item_id != row.fact_id:
                raise ValueError("item event reference mismatch")
    items_by_turn: dict[UUID, list[FactRowBase]] = {}
    for row in items.values():
        items_by_turn.setdefault(row.parent_id, []).append(row)
    validate_sequences(items_by_turn, label="item")

    events_by_session: dict[UUID, list[FactRowBase]] = {}
    for row in events.values():
        event = row.payload
        if row.fact_id != event.event_id:
            raise ValueError("event fact identity mismatch")
        if event.turn_id is not None:
            turn = turns.get(event.turn_id)
            if turn is None or row.parent_id != event.turn_id:
                raise ValueError("event turn ownership mismatch")
        elif row.parent_id not in sessions:
            raise ValueError("session event ownership mismatch")
        session_id = turns[event.turn_id].parent_id if event.turn_id else row.parent_id
        events_by_session.setdefault(session_id, []).append(row)
        if event.item_id is not None:
            item = items.get(event.item_id)
            if (
                item is None
                or event.event_id not in item.payload.event_ids
                or item.parent_id != event.turn_id
            ):
                raise ValueError("event item ownership mismatch")
    validate_sequences(events_by_session, label="event")

    for row in by_kind["request"].values():
        if row.fact_id != row.payload.request_id or row.parent_id not in turns:
            raise ValueError("request fact identity mismatch")

    for row in [*by_kind["runtime"].values(), *by_kind["measurement"].values()]:
        if row.parent_id not in sessions:
            raise ValueError(f"{row.kind} session ownership mismatch")

    for row in by_kind["model"].values():
        if row.parent_id != graph_id:
            raise ValueError("model graph ownership mismatch")

    for row in by_kind["output_evidence"].values():
        item = items.get(row.fact_id)
        if item is None or row.parent_id != row.fact_id:
            raise ValueError("output evidence item identity mismatch")
        if not set(row.payload.source_event_ids) <= set(item.payload.event_ids):
            raise ValueError(
                "output evidence references an event the item does not own"
            )
        for event_id in row.payload.source_event_ids:
            event = events.get(event_id)
            if event is None or event.payload.item_id != row.fact_id:
                raise ValueError("output evidence event reference mismatch")

    edge_identities: set[tuple[str, UUID, UUID, UUID | None, UUID | None]] = set()
    for row in by_kind["edge"].values():
        edge = row.payload
        if (
            row.parent_id != graph_id
            or edge.source_session_id not in sessions
            or edge.target_session_id not in sessions
            or edge.origin.session_id != edge.source_session_id
        ):
            raise ValueError("edge session ownership mismatch")
        if edge.origin.turn_id is not None:
            turn = turns.get(edge.origin.turn_id)
            if turn is None or turn.parent_id != edge.source_session_id:
                raise ValueError("edge turn ownership mismatch")
        if edge.origin.item_id is not None:
            item = items.get(edge.origin.item_id)
            if item is None or item.parent_id != edge.origin.turn_id:
                raise ValueError("edge item ownership mismatch")
        identity = (
            edge.kind,
            edge.source_session_id,
            edge.target_session_id,
            edge.origin.turn_id,
            edge.origin.item_id,
        )
        if identity in edge_identities:
            raise ValueError("fact set contains duplicate edges")
        edge_identities.add(identity)
        referenced_events = [
            *edge.evidence_event_ids,
            *([edge.origin.event_id] if edge.origin.event_id is not None else []),
        ]
        for event_id in referenced_events:
            event = events.get(event_id)
            expected_parent = edge.origin.turn_id or edge.source_session_id
            if (
                event is None
                or event.parent_id != expected_parent
                or event.payload.item_id != edge.origin.item_id
            ):
                raise ValueError("edge event ownership mismatch")

    summary = graph_row.payload.summary
    if (
        summary.session_count != len(sessions)
        or summary.turn_count != len(turns)
        or summary.item_count != len(items)
    ):
        raise ValueError("graph summary fact counts mismatch")


def _row(
    cls: type[FactRowBase],
    *,
    graph_id: UUID,
    fact_id: UUID,
    parent_id: UUID | None,
    order_index: int | None,
    payload: Any,
) -> Any:
    kind = cls.model_fields["kind"].annotation
    view = {
        "kind": getattr(kind, "__args__", [None])[0],
        "graph_id": str(graph_id),
        "fact_id": str(fact_id),
        "parent_id": str(parent_id) if parent_id is not None else None,
        "order_index": order_index,
        "payload": (
            payload.model_dump(mode="json", exclude_none=True)
            if isinstance(payload, BaseModel)
            else payload
        ),
    }
    view = {key: value for key, value in view.items() if value is not None}
    return cls(**{**view, "row_hash": compute_row_hash(view)})


class PublishedFactSet(FactModel):
    """One graph's complete, deterministic, bounded publication facts."""

    schema_version: Literal["ct.published_facts.v2"] = FACT_SET_SCHEMA_VERSION
    graph_id: UUID
    fact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind_counts: dict[str, int]
    rows: list[FactRow] = Field(max_length=MAX_FACT_ROWS_PER_GRAPH)

    @classmethod
    def from_rows(cls, graph_id: UUID, rows: list[FactRowBase]) -> PublishedFactSet:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.kind] = counts.get(row.kind, 0) + 1
        return cls(
            graph_id=graph_id,
            fact_set_digest=compute_fact_set_digest(graph_id, rows),
            kind_counts=counts,
            rows=rows,
        )

    @model_validator(mode="after")
    def validate_integrity(self) -> PublishedFactSet:
        if not self.rows:
            raise ValueError("fact set requires at least one row")
        for row in self.rows:
            if row.graph_id != self.graph_id:
                raise ValueError("fact row graph mismatch")
            if compute_row_hash(row.hashable_view()) != row.row_hash:
                raise ValueError("fact row hash mismatch")
            encoded_row = canonical_json(
                row.model_dump(mode="json", exclude_none=True)
            ).encode()
            if len(encoded_row) > MAX_FACT_ROW_BYTES:
                raise ValueError("fact row exceeds the 512 KiB bound")
        ordered = sorted(self.rows, key=lambda row: (row.kind, str(row.fact_id)))
        if [(row.kind, str(row.fact_id)) for row in self.rows] != [
            (row.kind, str(row.fact_id)) for row in ordered
        ]:
            raise ValueError("fact rows are not in canonical order")
        keys = [(row.kind, row.fact_id) for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("fact set contains duplicate facts")
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.kind] = counts.get(row.kind, 0) + 1
        if counts != self.kind_counts:
            raise ValueError("fact set kind counts mismatch")
        for kind, count in counts.items():
            limit = FACT_KIND_LIMITS.get(kind)
            if limit is not None and count > limit:
                raise ValueError(f"fact set exceeds the {kind} cardinality bound")
        present = {(row.kind, row.fact_id) for row in self.rows}
        parents = {
            "session": {"graph"},
            "turn": {"session"},
            "item": {"turn"},
            "event": {"turn", "session"},
            "output_evidence": {"item"},
            "request": {"turn"},
            "runtime": {"session"},
            "measurement": {"session"},
            "edge": {"graph"},
            "model": {"graph"},
        }
        for row in self.rows:
            expected = parents.get(row.kind)
            if expected is None:
                if row.parent_id is not None:
                    raise ValueError(f"fact row kind {row.kind} must not have a parent")
                continue
            if row.parent_id is None:
                raise ValueError(f"fact row {row.kind} requires a retained parent")
            if not any((kind, row.parent_id) in present for kind in expected):
                raise ValueError(f"fact row {row.kind} parent is not retained")
        _validate_fact_relationships(self.graph_id, list(self.rows))
        if compute_fact_set_digest(self.graph_id, list(self.rows)) != (
            self.fact_set_digest
        ):
            raise ValueError("fact set digest mismatch")
        dumped = self.model_dump(mode="json", exclude_none=True)
        encoded = canonical_json(dumped)
        if len(encoded.encode()) > MAX_FACT_SET_BYTES:
            raise ValueError("fact set exceeds the 16 MiB bound")
        _reject_embedded_content(dumped)
        return self


@dataclass(slots=True)
class FactIndex:
    """Non-semantic read indexes over typed local or published fact rows."""

    _canonical_rows: tuple[FactRowBase, ...]
    _rows_by_kind: dict[str, tuple[FactRowBase, ...]]
    _rows_by_id: dict[tuple[UUID, UUID], tuple[FactRowBase, ...]]
    _rows_by_parent: dict[tuple[UUID, UUID], tuple[FactRowBase, ...]]
    _rows_by_graph: dict[UUID, tuple[FactRowBase, ...]]
    _entrypoint_kinds: dict[tuple[UUID, UUID], frozenset[str]]
    _materialized_graphs: dict[UUID, SessionGraph] = dataclass_field(
        default_factory=dict
    )

    @classmethod
    def from_fact_sets(cls, fact_sets: list[PublishedFactSet]) -> FactIndex:
        graph_ids = [fact_set.graph_id for fact_set in fact_sets]
        if len(graph_ids) != len(set(graph_ids)):
            raise ValueError("fact index contains duplicate graphs")
        rows = tuple(
            row
            for fact_set in sorted(fact_sets, key=lambda value: str(value.graph_id))
            for row in fact_set.rows
        )
        return cls.from_rows(rows)

    @classmethod
    def from_rows(cls, rows: Iterable[FactRowBase]) -> FactIndex:
        """Index trusted typed rows without applying publication size budgets."""

        rows = tuple(rows)
        by_kind: dict[str, list[FactRowBase]] = {}
        by_id: dict[tuple[UUID, UUID], list[FactRowBase]] = {}
        by_parent: dict[tuple[UUID, UUID], list[FactRowBase]] = {}
        by_graph: dict[UUID, list[FactRowBase]] = {}
        entrypoints: dict[tuple[UUID, UUID], set[str]] = {}
        for row in rows:
            by_kind.setdefault(row.kind, []).append(row)
            by_id.setdefault((row.graph_id, row.fact_id), []).append(row)
            if row.parent_id is not None:
                by_parent.setdefault((row.graph_id, row.parent_id), []).append(row)
            by_graph.setdefault(row.graph_id, []).append(row)
            if row.kind in {"graph", "session", "turn"}:
                entrypoints.setdefault((row.graph_id, row.fact_id), set()).add(row.kind)
        return cls(
            _canonical_rows=rows,
            _rows_by_kind={kind: tuple(values) for kind, values in by_kind.items()},
            _rows_by_id={key: tuple(values) for key, values in by_id.items()},
            _rows_by_parent={key: tuple(values) for key, values in by_parent.items()},
            _rows_by_graph={
                graph_id: tuple(values) for graph_id, values in by_graph.items()
            },
            _entrypoint_kinds={
                key: frozenset(kinds) for key, kinds in entrypoints.items()
            },
        )

    @property
    def canonical_rows(self) -> tuple[FactRowBase, ...]:
        return self._canonical_rows

    @property
    def graph_ids(self) -> tuple[UUID, ...]:
        return tuple(self._rows_by_graph)

    def rows_of_kind(self, kind: str) -> tuple[FactRowBase, ...]:
        return self._rows_by_kind.get(kind, ())

    def rows_for_id(self, graph_id: UUID, fact_id: UUID) -> tuple[FactRowBase, ...]:
        return self._rows_by_id.get((graph_id, fact_id), ())

    def rows_for_parent(
        self, graph_id: UUID, parent_id: UUID
    ) -> tuple[FactRowBase, ...]:
        return self._rows_by_parent.get((graph_id, parent_id), ())

    def rows_for_graph(self, graph_id: UUID) -> tuple[FactRowBase, ...]:
        return self._rows_by_graph.get(graph_id, ())

    def row(self, graph_id: UUID, kind: str, fact_id: UUID) -> FactRowBase | None:
        return next(
            (row for row in self.rows_for_id(graph_id, fact_id) if row.kind == kind),
            None,
        )

    def payload(self, graph_id: UUID, kind: str, fact_id: UUID) -> Any | None:
        row = self.row(graph_id, kind, fact_id)
        return row.payload if row is not None else None

    def graph_id_for_entrypoint(self, entrypoint_id: UUID) -> UUID | None:
        if "graph" in self._entrypoint_kinds.get(
            (entrypoint_id, entrypoint_id), frozenset()
        ):
            return entrypoint_id
        for kind in ("session", "turn"):
            for graph_id in reversed(self.graph_ids):
                if kind in self._entrypoint_kinds.get(
                    (graph_id, entrypoint_id), frozenset()
                ):
                    return graph_id
        return None


def session_graph_from_fact_index(facts: FactIndex, graph_id: UUID) -> SessionGraph:
    """Materialize one selected canonical graph for existing semantic handlers."""

    cached = facts._materialized_graphs.get(graph_id)
    if cached is not None:
        return cached

    graph_row = facts.row(graph_id, "graph", graph_id)
    if graph_row is None:
        raise ValueError("fact index requires exactly one selected graph row")
    evidence_by_item = {
        row.fact_id: row.payload
        for row in facts.rows_for_graph(graph_id)
        if row.kind == "output_evidence"
    }

    sessions: list[ChronicleSession] = []
    for session_row in facts.rows_for_parent(graph_id, graph_id):
        if session_row.kind != "session":
            continue
        session_payload = session_row.payload
        assert isinstance(session_payload, SessionFactPayload)
        sid = session_payload.session_id
        turns: list[ChronicleTurn] = []
        events = [
            row.payload
            for row in facts.rows_for_parent(graph_id, sid)
            if row.kind == "event"
        ]
        runtime = [
            row.payload
            for row in sorted(
                facts.rows_for_parent(graph_id, sid),
                key=lambda row: (row.order_index or 0, str(row.fact_id)),
            )
            if row.kind == "runtime"
        ]
        measurement_row = next(
            (
                row
                for row in facts.rows_for_parent(graph_id, sid)
                if row.kind == "measurement"
            ),
            None,
        )
        for turn_row in facts.rows_for_parent(graph_id, sid):
            if turn_row.kind != "turn":
                continue
            turn_payload = turn_row.payload
            assert isinstance(turn_payload, TurnFactPayload)
            items = [
                ChronicleItem(
                    **item_row.payload.model_dump(mode="python"),
                    output_evidence=evidence_by_item.get(item_row.fact_id),
                )
                for item_row in facts.rows_for_parent(graph_id, turn_row.fact_id)
                if item_row.kind == "item"
            ]
            requests = [
                row.payload
                for row in sorted(
                    facts.rows_for_parent(graph_id, turn_row.fact_id),
                    key=lambda row: (row.order_index or 0, str(row.fact_id)),
                )
                if row.kind == "request"
            ]
            events.extend(
                row.payload
                for row in facts.rows_for_parent(graph_id, turn_row.fact_id)
                if row.kind == "event"
            )
            turns.append(
                ChronicleTurn(
                    **turn_payload.model_dump(mode="python"),
                    requests=requests,
                    items=sorted(items, key=lambda item: item.sequence),
                )
            )
        sessions.append(
            ChronicleSession(
                **session_payload.model_dump(mode="python"),
                runtime=runtime,
                measurements=(
                    measurement_row.payload
                    if measurement_row is not None
                    else ChronicleSessionMeasurements()
                ),
                events=sorted(events, key=lambda event: event.sequence),
                turns=sorted(turns, key=lambda turn: turn.sequence),
            )
        )
    graph_payload = graph_row.payload
    assert isinstance(graph_payload, GraphFactPayload)
    canonical_sessions = [_to_session(session) for session in sessions]
    result = SessionGraph(
        root_session_id=graph_payload.summary.root_session_id,
        project_identifier=graph_payload.summary.project,
        summary=SessionGraphSummary(
            root_session_id=graph_payload.summary.root_session_id,
            started_at=graph_payload.summary.started_at,
            ended_at=graph_payload.summary.ended_at,
            session_count=graph_payload.summary.session_count,
            turn_count=graph_payload.summary.turn_count,
            vendors=sorted(
                {session.vendor for session in canonical_sessions},
                key=lambda vendor: vendor.value,
            ),
        ),
        edges=[
            _to_edge(row.payload)
            for row in facts.rows_for_parent(graph_id, graph_id)
            if row.kind == "edge"
        ],
        sessions=canonical_sessions,
    )
    facts._materialized_graphs[graph_id] = result
    return result


def _assemble_published_fact_set(
    *,
    summary: ChronicleGraphSummary,
    sessions: list[ChronicleSession],
    edges: list[ChronicleEdge],
    coverage: ChronicleCoverage,
) -> PublishedFactSet:
    """Assemble already-projected payloads into the bounded publication contract."""

    return PublishedFactSet.from_rows(
        summary.root_session_id,
        _assemble_fact_rows(
            summary=summary, sessions=sessions, edges=edges, coverage=coverage
        ),
    )


def _assemble_fact_rows(
    *,
    summary: ChronicleGraphSummary,
    sessions: list[ChronicleSession],
    edges: list[ChronicleEdge],
    coverage: ChronicleCoverage,
) -> list[FactRowBase]:
    """Assemble every projected fact; publication budgets belong to the wrapper."""

    graph_id = summary.root_session_id
    rows: list[FactRowBase] = [
        _row(
            GraphFactRow,
            graph_id=graph_id,
            fact_id=graph_id,
            parent_id=None,
            order_index=None,
            payload=GraphFactPayload(
                summary=summary,
                coverage=coverage,
            ),
        )
    ]

    for session in sessions:
        sid = session.session_id
        rows.append(
            _row(
                SessionFactRow,
                graph_id=graph_id,
                fact_id=sid,
                parent_id=graph_id,
                order_index=None,
                payload=SessionFactPayload(
                    session_id=sid,
                    parent_session_id=session.parent_session_id,
                    vendor=session.vendor,
                    started_at=session.started_at,
                    ended_at=session.ended_at,
                    status=session.status,
                    model=session.model,
                    reasoning_effort=session.reasoning_effort,
                    title=session.title,
                    preview=session.preview,
                    agent_name=session.agent_name,
                    cwd=session.cwd,
                    agent_path=session.agent_path,
                    topology=session.topology,
                ),
            )
        )
        rows.append(
            _row(
                MeasurementFactRow,
                graph_id=graph_id,
                fact_id=uuid5(_FACT_NAMESPACE, f"measurements:{sid}"),
                parent_id=sid,
                order_index=None,
                payload=session.measurements,
            )
        )
        for position, observation in enumerate(session.runtime):
            rows.append(
                _row(
                    RuntimeFactRow,
                    graph_id=graph_id,
                    fact_id=uuid5(_FACT_NAMESPACE, f"runtime:{sid}:{position}"),
                    parent_id=sid,
                    order_index=position,
                    payload=observation,
                )
            )
        for event in session.events:
            rows.append(
                _row(
                    EventFactRow,
                    graph_id=graph_id,
                    fact_id=event.event_id,
                    parent_id=event.turn_id or sid,
                    order_index=event.sequence,
                    payload=event,
                )
            )
        for turn in session.turns:
            rows.append(
                _row(
                    TurnFactRow,
                    graph_id=graph_id,
                    fact_id=turn.turn_id,
                    parent_id=sid,
                    order_index=turn.sequence,
                    payload=TurnFactPayload(
                        turn_id=turn.turn_id,
                        sequence=turn.sequence,
                        started_at=turn.started_at,
                        completed_at=turn.completed_at,
                        status=turn.status,
                        user_request=turn.user_request,
                        team_state=turn.team_state,
                    ),
                )
            )
            for position, request in enumerate(turn.requests):
                rows.append(
                    _row(
                        RequestFactRow,
                        graph_id=graph_id,
                        fact_id=request.request_id,
                        parent_id=turn.turn_id,
                        order_index=position,
                        payload=request,
                    )
                )
            for item in turn.items:
                evidence = item.output_evidence
                rows.append(
                    _row(
                        ItemFactRow,
                        graph_id=graph_id,
                        fact_id=item.item_id,
                        parent_id=turn.turn_id,
                        order_index=item.sequence,
                        payload=ItemFactPayload(
                            **item.model_dump(
                                mode="python", exclude={"output_evidence"}
                            )
                        ),
                    )
                )
                if evidence is not None:
                    rows.append(
                        _row(
                            OutputEvidenceFactRow,
                            graph_id=graph_id,
                            fact_id=item.item_id,
                            parent_id=item.item_id,
                            order_index=None,
                            payload=evidence,
                        )
                    )

    for position, edge in enumerate(edges):
        rows.append(
            _row(
                EdgeFactRow,
                graph_id=graph_id,
                fact_id=uuid5(
                    _FACT_NAMESPACE,
                    "edge:"
                    + canonical_json(
                        {
                            "kind": edge.kind,
                            "source": str(edge.source_session_id),
                            "target": str(edge.target_session_id),
                            "turn": str(edge.origin.turn_id or ""),
                            "item": str(edge.origin.item_id or ""),
                        }
                    ),
                ),
                parent_id=graph_id,
                order_index=position,
                payload=edge,
            )
        )

    rows.extend(_model_fact_rows(sessions, graph_id=graph_id))
    rows.sort(key=lambda row: (row.kind, str(row.fact_id)))
    return rows


def _model_fact_rows(
    sessions: list[ChronicleSession], *, graph_id: UUID
) -> list[FactRowBase]:
    """Roll up one queryable model fact per (model, provider set) per graph."""

    grouped: dict[str, dict[str, Any]] = {}
    for session in sessions:
        for turn in session.turns:
            for request in turn.requests:
                key = request.model or ""
                entry = grouped.setdefault(
                    key,
                    {
                        "model": request.model,
                        "providers": set(),
                        "request_count": 0,
                        "usage": {
                            "input_tokens": 0,
                            "cached_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 0,
                        },
                        "cost": Decimal(0),
                        "cost_seen": False,
                    },
                )
                if request.provider:
                    entry["providers"].add(request.provider)
                entry["request_count"] += 1
                for field in entry["usage"]:
                    entry["usage"][field] += getattr(request.usage, field)
                if request.usage.cost_usd is not None:
                    entry["cost"] += Decimal(request.usage.cost_usd)
                    entry["cost_seen"] = True
    rows: list[FactRowBase] = []
    for key in sorted(grouped):
        entry = grouped[key]
        usage = dict(entry["usage"])
        if entry["cost_seen"]:
            usage["cost_usd"] = str(entry["cost"])
        rows.append(
            _row(
                ModelFactRow,
                graph_id=graph_id,
                fact_id=uuid5(_FACT_NAMESPACE, f"model:{graph_id}:{key}"),
                parent_id=graph_id,
                order_index=None,
                payload=ModelFactPayload(
                    model=entry["model"],
                    providers=sorted(entry["providers"]),
                    request_count=entry["request_count"],
                    usage=ChronicleUsage(**usage),
                ),
            )
        )
    return rows


__all__ = [
    "DERIVED_FACT_KINDS",
    "FACT_KIND_LIMITS",
    "FACT_SET_SCHEMA_VERSION",
    "MAX_FACT_READ_PAGE_BYTES",
    "MAX_FACT_ROW_BYTES",
    "FactIndex",
    "FactRow",
    "FactRowBase",
    "GraphFactPayload",
    "ItemFactPayload",
    "PublishedFactSet",
    "SessionFactPayload",
    "TurnFactPayload",
    "compute_fact_set_digest",
    "compute_row_hash",
    "session_graph_from_fact_index",
]
