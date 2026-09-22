"""Qualify deterministic collector preparation through real journals and SQLite."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from threading import Barrier, Lock, get_ident
from unittest.mock import patch
from uuid import UUID

import httpx
from coding_trajectory.analysis.activity_flow import build_overview_flows
from coding_trajectory.control_plane import graph_preparation
from coding_trajectory.control_plane.artifact_protocol import (
    ARTIFACT_PREPARATION_VERSION,
    ArtifactPublicationRequest,
    ArtifactReadinessResponse,
)
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
    CollectorRemoteError,
    LocalCollector,
)
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryResponse,
    ObservationReceipt,
    ObservationRequest,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.fact_projection import build_published_fact_set
from coding_trajectory.control_plane.fact_repository import LocalPublishedFactRepository
from coding_trajectory.control_plane.graph_preparation import (
    graph_input_digest,
    prepare_graph,
)
from coding_trajectory.control_plane.prepared_api_reader import (
    read_prepared,
    save_local_view,
)
from coding_trajectory.control_plane.published_facts import (
    FactIndex,
    session_graph_from_fact_index,
)
from coding_trajectory.discovery import discover_store_from_files
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.ingestion.models import CommandExecutionItem, ToolCallItem
from coding_trajectory.project_identity import local_project_id
from coding_trajectory.runtime import ServiceRuntime
from coding_trajectory.service.store import IndexCache
from coding_trajectory_cli.commands.session import _render_session_overview_text


def qualify_semantic_details(graph, root: Path) -> None:
    """Exercise richer descriptions through local preparation and fact replay."""
    graph = graph.model_copy(deep=True)
    session = graph.sessions[0]
    turn = session.turns[0]
    descriptions = [
        "python3 validate_config.py",
        "custom-check /home/synthetic/work/config.toml",
        "python3 inspect.py --token synthetic-credential",
        "python3 inspect.py token budget",
        "curl -H 'Authorization: Bearer synthetic-bearer' https://user:pass@example.com/a?token=hidden",
    ]
    turn.items = [
        CommandExecutionItem(
            item_id=UUID(int=900 + index),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence=index,
            started_at=turn.started_at,
            status="failed" if index == 0 else "completed",
            command=description,
        )
        for index, description in enumerate(descriptions)
    ]
    cache_path = root / "semantic-preparation.sqlite"
    prepared = prepare_graph(graph, cache_path=cache_path)
    assert ARTIFACT_PREPARATION_VERSION == "ct.graph-preparation.v6"
    assert prepared.summary.preparation_version == ARTIFACT_PREPARATION_VERSION
    facts = prepared.publication()
    reconstructed = session_graph_from_fact_index(
        FactIndex.from_fact_sets([facts]), graph.root_session_id
    )
    summaries = [
        item.measurements.tool_summary
        for item in reconstructed.sessions[0].turns[0].items
    ]
    assert [summary["description"] for summary in summaries[:4]] == [
        descriptions[0],
        descriptions[1],
        "python3 inspect.py --token [redacted]",
        descriptions[3],
    ]
    encoded = facts.model_dump_json()
    assert "synthetic-credential" not in encoded and "synthetic-bearer" not in encoded
    assert "user:pass" not in encoded and "token=hidden" not in encoded
    assert (
        build_overview_flows([reconstructed.sessions[0].turns[0].items[0]])[0]["cmd"]
        == descriptions[0]
    )
    # A valid old cache entry with a deliberately empty row set must not win
    # over the current producer version for the same canonical graph digest.
    with sqlite3.connect(cache_path) as db:
        db.execute("DELETE FROM prepared")
        old = prepared.model_dump(mode="json")
        old["rows"] = []
        old["summary"]["preparation_version"] = "ct.graph-preparation.v5"
        db.execute(
            "INSERT INTO prepared VALUES (?, ?)",
            (
                "ct.graph-preparation.v5:" + graph_input_digest(graph),
                json.dumps(old),
            ),
        )
    assert prepare_graph(graph, cache_path=cache_path) == prepared
    assert prepare_graph(graph, cache_path=cache_path) == prepared
    print(
        "PASS semantic detail publication/replay, credential redaction, and v5 cache invalidation"
    )
    # More than eight later noise records must not displace useful activities.
    turn.items = turn.items + [
        ToolCallItem(
            item_id=UUID(int=1000 + n),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence=5 + n,
            started_at=turn.started_at,
            status=status,
            tool_name=name,
            input=arguments,
        )
        for n, (name, arguments, status) in enumerate(
            [
                ("Read", {"file_path": "/work/openai-docs/SKILL.md"}, "completed"),
                ("WebSearch", {"query": "Codex scheduling documentation"}, "completed"),
                ("WebFetch", {"url": "https://example.com/docs"}, "completed"),
                ("WebFetch", {"url": "https://example.com/failure"}, "failed"),
                *[("exec", {}, "completed")] * 9,
                ("WebFetch", {}, "completed"),
                ("WebFetch", {}, "failed"),
            ]
        )
    ]
    meaningful_ids = [str(item.item_id) for item in turn.items[:9]]
    noisy_items = turn.items
    prefix = [
        ToolCallItem(
            item_id=UUID(int=2000 + n),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence=n,
            started_at=turn.started_at,
            tool_name="WebFetch",
            input={"url": f"https://example.com/earlier/{n}"},
            status="completed",
        )
        for n in range(4)
    ]
    turn.items = prefix + noisy_items
    for sequence, item in enumerate(turn.items):
        item.sequence = sequence
    canonical_ids = [str(item.item_id) for item in turn.items]
    prepared = prepare_graph(graph, cache_path=cache_path)
    scope = str(session.session_id)

    def read(method, params):
        descriptor = next(
            m
            for m in prepared.api.methods
            if m.method == method and m.scope == scope and m.turn_id is None
        )
        return read_prepared(
            descriptor,
            {"session_id": scope, **params},
            identity=save_local_view(prepared.api, prepared.summary.fact_set_digest),
            fetch=lambda digest: prepared.api.objects[digest].encode(),
            signing_key=b"k" * 32,
        )

    overview = read("session.overview", {"limit": 10})
    projected = next(
        row for row in overview["turns"] if row["turn_id"] == str(turn.turn_id)
    )
    canonical = session_graph_from_fact_index(
        FactIndex.from_rows(prepared.rows), graph.root_session_id
    )
    canonical_turn = next(
        t for s in canonical.sessions for t in s.turns if t.turn_id == turn.turn_id
    )
    expected = [
        flow
        for flow in build_overview_flows(canonical_turn.items, flatten_commands=True)
        if "tool" in flow
    ]
    assert projected["activities"] == expected[-8:]
    assert [cell["item_ids"] for cell in projected["activities"]] == [
        [item_id] for item_id in meaningful_ids[1:]
    ]
    assert all("count" not in cell for cell in projected["activities"])
    assert projected["content_coverage"]["activities"] == {
        "total": 13,
        "returned": 8,
        "truncated": True,
    }
    assert projected["refs"]["item_ids"] == canonical_ids
    detail = read("session.items", {"turn_id": str(turn.turn_id), "limit": 100})
    assert [row["item_id"] for row in detail["items"]] == canonical_ids
    rendered = _render_session_overview_text(overview)
    activity_lines = [line for line in rendered.splitlines() if line.startswith("- ")]
    assert len(activity_lines) == 8
    assert "Ran 4 commands" not in rendered
    for line, item_id in zip(activity_lines[:4], meaningful_ids[1:5], strict=True):
        assert "RunCommand:" in line and item_id in line
    print("CLI flattened commands:", *activity_lines[:4], sep="\n")
    # Identical consecutive commands must not be remerged by compact projection.
    repeated = [canonical_turn.items[5].model_copy(deep=True) for _ in range(2)]
    repeated[1].item_id = UUID(int=3000)
    flat = build_overview_flows(repeated, flatten_commands=True)
    assert [cell["item_ids"] for cell in flat] == [
        [str(item.item_id)] for item in repeated
    ]
    # Below the cap the reported reproduction retains only the read and search.
    turn.items = noisy_items[5:7] + noisy_items[9:]
    prepared = prepare_graph(graph, cache_path=cache_path)
    overview = read("session.overview", {"limit": 10})
    projected = next(
        row for row in overview["turns"] if row["turn_id"] == str(turn.turn_id)
    )
    assert [row["item_ids"] for row in projected["activities"]] == [
        [item_id] for item_id in meaningful_ids[5:7]
    ]
    assert projected["content_coverage"]["activities"] == {
        "total": 2,
        "returned": 2,
        "truncated": False,
    }
    rendered = _render_session_overview_text(overview)
    activity_lines = [line for line in rendered.splitlines() if line.startswith("- ")]
    assert len(activity_lines) == 2
    assert "SKILL.md" in activity_lines[0]
    assert "Codex scheduling documentation" in activity_lines[1]
    print("CLI meaningful activities:", *activity_lines, sep="\n")
    print(
        "PASS flat command projection, repeated commands, cap/coverage/membership, meaningful WebFetch retained, and hidden canonical items preserved"
    )


class CheckpointRemote:
    def __init__(self) -> None:
        self.registrations: list[SourceRegistrationRequest] = []
        self.requests: list[ObservationRequest] = []
        self.recoveries = 0
        self.uploads = 0
        self.publications = 0

    def recover(self, _request) -> CollectorRecoveryResponse:
        self.recoveries += 1
        return CollectorRecoveryResponse(next_publication_sequence=0)

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse:
        assert idempotency_key
        self.registrations.append(request)
        return SourceRegistrationResponse(
            source_id=UUID(int=100 + len(self.registrations)),
            source_epoch=request.source_epoch,
        )

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
    ) -> ObservationReceipt:
        assert idempotency_key
        self.requests.append(ObservationRequest.model_validate(request.model_dump()))
        return ObservationReceipt(
            receipt_id=UUID(int=200 + len(self.requests)),
            outcome="accepted",
        )

    def upload_artifact(self, *, kind: str, sha256: str, body: bytes) -> None:
        assert kind in {"facts", "summary", "api"} and len(sha256) == 64 and body
        self.uploads += 1

    def artifact_readiness(self, request):
        return ArtifactReadinessResponse(ready=[False] * len(request.objects))

    def publish_artifacts(self, request, *, idempotency_key: str) -> ObservationReceipt:
        assert request.graphs and idempotency_key
        self.publications += 1
        if self.publications == 1:
            raise CollectorRemoteError("synthetic uncertain artifact response")
        return ObservationReceipt(
            receipt_id=UUID(int=300 + self.publications), outcome="accepted"
        )


def qualify_upload_scheduling(collector) -> None:
    from coding_trajectory.contracts.prepared_api import PreparedObject

    row = collector._connection.execute(
        "SELECT request_json FROM publication_outbox LIMIT 1"
    ).fetchone()
    request = ArtifactPublicationRequest.model_validate(
        json.loads(row[0])["artifact_publication"]
    )
    extra = []
    for ordinal in range(521):
        body = json.dumps({"synthetic": ordinal}).encode()
        digest = hashlib.sha256(body).hexdigest()
        extra.append(PreparedObject(sha256=digest, bytes=len(body)))
        collector._connection.execute(
            "INSERT INTO artifact_objects VALUES (?, 'api', ?, ?)",
            (digest, body, len(body)),
        )
    request.graphs[0].api_objects.extend(extra)
    request.graphs.append(request.graphs[0])  # Repeated refs must upload only once.
    missing = {ref.sha256 for ref in extra[:8]}
    owner = get_ident()

    class Authority:
        def __init__(self, fail=False):
            self.fail, self.calls, self.batches = fail, [], []
            self.completed = set()
            self.active = self.maximum = 0
            self.lock, self.barrier = Lock(), Barrier(4, timeout=10)

        def artifact_readiness(self, query):
            assert get_ident() == owner
            self.batches.append(query.objects)
            return ArtifactReadinessResponse(
                ready=[
                    ref.sha256 not in missing or ref.sha256 in self.completed
                    for ref in query.objects
                ]
            )

        def upload_artifact(self, *, kind, sha256, body):
            assert get_ident() != owner and kind == "api"
            assert hashlib.sha256(body).hexdigest() == sha256
            with self.lock:
                self.calls.append(sha256)
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                self.barrier.wait()
                if self.fail and sha256 == extra[0].sha256:
                    raise CollectorRemoteError("synthetic uncertain upload")
                with self.lock:
                    self.completed.add(sha256)
            finally:
                with self.lock:
                    self.active -= 1

    authority = Authority()
    collector._upload_artifacts(authority, request)
    assert len(authority.calls) == 8 and set(authority.calls) == missing
    assert authority.maximum == 4 and authority.active == 0
    assert len(authority.batches) == 2 and len(authority.batches[0]) == 512
    assert sum(len(batch) for batch in authority.batches) == len(
        {
            (ref.kind, ref.sha256)
            for graph in request.graphs
            for ref in (graph.facts, graph.summary, *graph.api_objects)
        }
    )
    indexes = {
        method.index.sha256
        for graph in request.graphs
        for method in graph.api_methods
        if method.index
    }
    assert {
        ref.sha256 for batch in authority.batches for ref in batch if ref.requires_index
    } == indexes
    collector._upload_artifacts(authority, request)
    assert len(authority.calls) == 8  # Fresh authority evidence, not local receipts.
    failing = Authority(fail=True)
    try:
        collector._upload_artifacts(failing, request)
    except CollectorRemoteError:
        pass
    else:
        raise AssertionError("upload failure swallowed")
    assert (
        len(failing.calls) == 4 and len(failing.completed) == 3 and failing.active == 0
    )
    # Old Workers and malformed readiness responses must stop before any PUT.
    remote = CloudflareCollectorRemote(url="http://localhost", access_token="synthetic")
    remote._client.close()
    for status, payload in (
        (400, {"error": {"code": "unknown_method"}}),
        (200, {"ok": True, "data": {"ready": [True]}}),
        (200, {"ok": True, "data": {"ready": ["true"] * 512}}),
    ):
        calls = []

        def respond(message, calls=calls, status=status, payload=payload):
            calls.append(message.method)
            return httpx.Response(status, json=payload)

        remote._client = httpx.Client(transport=httpx.MockTransport(respond))
        try:
            collector._upload_artifacts(remote, request)
        except (CollectorRemoteError, ValueError):
            pass
        else:
            raise AssertionError("invalid readiness accepted")
        finally:
            remote.close()
        assert calls == ["POST"]
    for ref in extra:
        collector._connection.execute(
            "DELETE FROM artifact_objects WHERE sha256=?", (ref.sha256,)
        )
    collector._connection.commit()
    print(
        "PASS 512-reference batching, deduplication, authority reuse, four concurrent uploads, failure drain and no later scheduling"
    )


def qualify_upload_transport() -> None:
    bodies = [b"x" * size for size in (65_535, 65_536, 65_537)]
    bodies.append(json.dumps({"text": "é🚀" * 800_000}, ensure_ascii=False).encode())
    received = []

    class UploadTransport(httpx.BaseTransport):
        def handle_request(self, request):
            chunks = list(request.stream)
            assert max(map(len, chunks)) <= 65_536
            body = b"".join(chunks)
            assert request.method == "PUT"
            assert request.headers["content-length"] == str(len(body))
            assert request.headers["content-type"] == "application/json"
            assert "transfer-encoding" not in request.headers
            assert request.url.path.endswith(hashlib.sha256(body).hexdigest())
            assert set(request.extensions["timeout"].values()) == {120}
            received.append(body)
            return httpx.Response(200, json={"ok": True})

    remote = CloudflareCollectorRemote(
        url="http://localhost", access_token="synthetic", timeout=120
    )
    remote._client.close()
    remote._client = httpx.Client(transport=UploadTransport())
    try:
        for body in bodies:
            remote.upload_artifact(
                kind="facts", sha256=hashlib.sha256(body).hexdigest(), body=body
            )
        assert received == bodies
    finally:
        remote.close()
    print(
        "PASS bounded upload writes, exact UTF-8 bytes/hash/Content-Length, no chunked encoding, unchanged 120s operation timeouts"
    )


def qualify_publication_transport(root, identity, publication_row) -> None:
    bound = 3 * 1024 * 1024
    worker = (
        Path(__file__).resolve().parents[1] / "cloudflare/control-plane/src/shared.py"
    )
    module = ast.parse(worker.read_text(), filename=str(worker))

    def integer_expression(node: ast.expr) -> int:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return integer_expression(node.left) * integer_expression(node.right)
        raise AssertionError("Worker MAX_BODY must be a static integer expression")

    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "MAX_BODY"
            for target in node.targets
        )
    )
    assert integer_expression(assignment.value) == bound
    method = "ct_collector_publish_artifacts"
    key = "transport-boundary"
    params = {"padding": "雪"}

    def expected_body():
        return httpx.Request(
            "POST",
            "http://localhost/v1/core",
            json={
                "protocol": "ct.core.v1",
                "id": None,
                "method": method,
                "params": params,
                "idempotency_key": key,
                "request_sha256": hashlib.sha256(
                    canonical_json(params).encode()
                ).hexdigest(),
            },
        ).content

    params["padding"] += "x" * (bound - len(expected_body()) - 1)
    sent = []

    def respond(request):
        assert request.headers["content-type"] == "application/json"
        sent.append(request.content)
        return httpx.Response(200, json={"ok": True, "data": {}})

    remote = CloudflareCollectorRemote(url="http://localhost", access_token="synthetic")
    remote._client.close()
    remote._client = httpx.Client(
        transport=httpx.MockTransport(respond),
        headers={"Content-Type": "application/json"},
    )
    try:
        for length in (bound - 1, bound, bound + 1):
            expected = expected_body()
            assert len(expected) == length
            try:
                remote._rpc(method, params, idempotency_key=key)
            except CollectorRemoteError as error:
                assert length == bound + 1
                assert error.code == "body_too_large" and error.status_code == 413
                assert str(length) in str(error) and str(bound) in str(error)
            else:
                assert length <= bound and sent[-1] == expected
            params["padding"] += "x"
        assert len(sent) == 2
    finally:
        remote.close()

    staged = json.loads(publication_row["request_json"])
    staged["artifact_publication"]["graphs"][0]["api_objects"].extend(
        {"kind": "api", "sha256": f"{number:064x}", "bytes": 128}
        for number in range(45_000)
    )
    row = dict(publication_row)
    row.update(request_json=json.dumps(staged), state="pending", last_error=None)
    authority = CheckpointRemote()
    with LocalCollector(
        database_path=root / "transport.sqlite3", identity=identity
    ) as collector:
        for attempts in (0, 1):
            row["attempts"] = attempts
            collector._connection.execute("DELETE FROM publication_outbox")
            collector._connection.execute(
                "INSERT INTO publication_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row.values()),
            )
            collector._connection.commit()
            before = tuple(
                collector._connection.execute(
                    "SELECT * FROM publication_outbox"
                ).fetchone()
            )
            try:
                collector._flush_facts(authority)
            except CollectorRemoteError as error:
                assert error.code == "body_too_large" and error.status_code == 413
            else:
                raise AssertionError("oversized publication reached artifact delivery")
            assert (
                tuple(
                    collector._connection.execute(
                        "SELECT * FROM publication_outbox"
                    ).fetchone()
                )
                == before
            )
        assert authority.uploads == authority.publications == authority.recoveries == 0
        # An old v2 request may already have committed before the collector
        # upgraded. Its receipt must settle the outbox without a v3 replay.
        collector._connection.execute("DELETE FROM publication_outbox")
        row = dict(publication_row)
        row.update(state="pending", attempts=1, last_error=None)
        collector._connection.execute(
            "INSERT INTO publication_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.values()),
        )
        collector._connection.commit()
        recovered = ObservationReceipt(receipt_id=UUID(int=123), outcome="accepted")
        authority.recover = lambda request: CollectorRecoveryResponse(
            next_publication_sequence=1,
            publication_receipt=recovered.model_dump(mode="json"),
        )
        assert collector._flush_facts(authority) == (1, 0)
        assert authority.uploads == authority.publications == 0
        assert (
            collector._connection.execute(
                "SELECT state FROM publication_outbox"
            ).fetchone()[0]
            == "accepted"
        )
    print(
        "PASS exact UTF-8 RPC boundary, unchanged idempotency bytes, pre-upload durable rejection, and v2 receipt recovery without replay"
    )


def qualify_pinned_publication(root: Path, journal: Path) -> None:
    """Execute the durable run against synthetic HTTP authority responses."""
    from coding_trajectory.control_plane import publication_run as runs
    from coding_trajectory.control_plane.artifact_protocol import (
        ArtifactManifestGraph,
        ArtifactPublicationRequest,
        compact_graph,
        expand_graph,
    )
    from coding_trajectory.control_plane.connections import (
        CollectorCredentialProfile,
        CollectorCredentials,
    )

    original = journal.read_bytes()
    source_sha = "a" * 40
    worker = UUID(int=55)
    credentials = CollectorCredentials(
        profile=CollectorCredentialProfile(
            cloudflare_url="http://localhost",
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            project_id=UUID(int=4),
            token_env="SYNTHETIC_TOKEN",
        ),
        access_token="synthetic-token-not-for-audit",
    )

    class Authority:
        def __init__(self, failure=None):
            self.failure = failure
            self.writes = []
            self.source = None
            self.receipt = None
            self.publication = None
            self.objects = {}
            self.version = str(worker)
            self.corrupt_manifest = False

        def __call__(self, request):
            if request.method == "PUT":
                self.writes.append("upload")
                self.objects[request.url.path] = request.content
                if self.failure == "upload":
                    self.failure = None
                    raise httpx.ReadTimeout(
                        "synthetic secret must not leak", request=request
                    )
                return httpx.Response(
                    200, headers={"X-CT-Worker-Version": self.version}
                )
            envelope = json.loads(request.content)
            method, params = envelope["method"], envelope["params"]
            data = {}
            if method == "ct_connection_status":
                data = {
                    "workspace_id": str(UUID(int=1)),
                    "agent_id": str(UUID(int=2)),
                    "roles": ["read", "collect"],
                }
            elif method == "ct_collector_recover":
                data = {
                    "next_publication_sequence": int(self.receipt is not None),
                    "source": self.source if params.get("native_session_id") else None,
                    "publication_receipt": self.receipt
                    if params.get("publication_idempotency_key")
                    else None,
                }
            elif method == "ct_collector_artifact_readiness":
                data = {
                    "ready": [
                        f"/v1/artifacts/{ref['kind']}/{ref['sha256']}" in self.objects
                        for ref in params["objects"]
                    ]
                }
            elif method == "ct_collector_register_source":
                self.writes.append("register")
                self.source = {
                    "source_id": str(UUID(int=101)),
                    "source_epoch": 1,
                    "next_source_sequence": 0,
                    "content_sha256": None,
                }
                data = {"source_id": self.source["source_id"], "source_epoch": 1}
            elif method == "ct_collector_publish_observation":
                self.writes.append("checkpoint")
                self.source.update(
                    next_source_sequence=params["source_sequence"] + 1,
                    content_sha256=params["content_sha256"],
                )
                data = {
                    "receipt_id": str(UUID(int=202)),
                    "outcome": "accepted",
                    "committed_sequence": 2,
                }
                if self.failure == "checkpoint":
                    self.failure = None
                    raise httpx.ReadTimeout("unknown checkpoint", request=request)
            elif method == "ct_collector_publish_artifacts":
                self.writes.append("manifest")
                self.publication = ArtifactPublicationRequest.model_validate(
                    {
                        **params,
                        "schema_version": "ct.artifact-manifest.v2",
                        "graphs": [expand_graph(g) for g in params["graphs"]],
                    }
                )
                self.receipt = {
                    "receipt_id": str(UUID(int=303)),
                    "outcome": "accepted",
                    "committed_sequence": 3,
                    "details": {"publication_outcome": "published"},
                }
                data = self.receipt
                if self.failure == "manifest":
                    self.failure = None
                    raise httpx.ReadTimeout("unknown manifest", request=request)
            elif method == "ct_artifact_manifest":
                graph_values = [
                    {
                        k: v
                        for k, v in graph.model_dump(mode="json").items()
                        if k in ArtifactManifestGraph.model_fields
                    }
                    for graph in self.publication.graphs
                ]
                if self.corrupt_manifest:
                    graph_values[0]["fact_count"] += 1
                data = {
                    "workspace_id": str(UUID(int=1)),
                    "snapshot_sequence": 3,
                    "manifests": [
                        {
                            "schema_version": "ct.artifact-manifest.v3",
                            "preparation_version": ARTIFACT_PREPARATION_VERSION,
                            "workspace_id": str(UUID(int=1)),
                            "project_id": str(UUID(int=4)),
                            "publisher_agent_id": str(UUID(int=2)),
                            "publication_sequence": 0,
                            "snapshot_sequence": 3,
                            "published_at": "2026-09-20T10:00:00.000Z",
                            "inventory_state": "complete",
                            "graphs": [compact_graph(g) for g in graph_values],
                        }
                    ],
                }
            else:
                raise AssertionError(method)
            return httpx.Response(
                200,
                headers={"X-CT-Worker-Version": self.version},
                json={"ok": True, "data": data},
            )

    def blocked(call):
        try:
            call()
        except (runs.PublicationStopped, CollectorRemoteError, ValueError):
            return
        raise AssertionError("unsafe operation was accepted")

    blocked(lambda: runs.source_pin("0" * 40))
    client = httpx.Client
    for scenario in (
        "success",
        "checkpoint",
        "upload",
        "manifest",
        "prefix",
        "discovery",
        "version",
        "workspace",
    ):
        authority = Authority(scenario)
        with (
            patch.object(runs, "source_pin", return_value="b" * 40),
            patch.object(runs, "load_profile_credentials", return_value=credentials),
            patch.object(
                httpx,
                "Client",
                side_effect=lambda authority=authority, **kw: client(
                    transport=httpx.MockTransport(authority), **kw
                ),
            ),
            runs.PublicationRun(root / f"pinned-{scenario}", create=True) as run,
        ):
            run.plan(
                source_sha=source_sha,
                worker_version=worker,
                credential_profile="synthetic",
                workspace_id=UUID(int=1),
                project_id=UUID(int=4),
                project_name="Checkpoint",
                project_root=root,
            )
            assert not authority.writes
            assert run.load().inventory[0].bytes == len(original)
            blocked(lambda: runs.PublicationRun(run.directory).__enter__())
            if scenario == "workspace":
                wrong = CollectorCredentials(
                    profile=credentials.profile.model_copy(
                        update={"workspace_id": UUID(int=99)}
                    ),
                    access_token=credentials.access_token,
                )
                with patch.object(runs, "load_profile_credentials", return_value=wrong):
                    blocked(run.execute)
                assert not authority.writes
                continue
            if scenario == "prefix":
                journal.write_bytes(
                    original.replace(b"Checkpoint evidence", b"Changed checkpoint!")
                )
                blocked(run.execute)
                assert not authority.writes
                journal.write_bytes(original)
                continue
            if scenario == "discovery":
                added = journal.with_name("new-source.jsonl")
                added.write_bytes(original)
                blocked(run.execute)
                assert not authority.writes
                added.unlink()
                continue
            if scenario == "version":
                authority.version = str(UUID(int=99))
                blocked(run.execute)
                assert not authority.writes
                continue
            if scenario == "success":
                # Growth after planning belongs to the next run, not this one.
                extra = json.loads(original.splitlines()[-1])
                extra["message"]["id"] = "future-message"
                extra["message"]["content"][0]["text"] = (
                    "Future content must not publish"
                )
                journal.write_bytes(original + json.dumps(extra).encode() + b"\n")
                assert run.execute()["state"] == "committed"
                assert not any(
                    b"Future content must not publish" in body
                    for body in authority.objects.values()
                )
                journal.write_bytes(original)
            else:
                blocked(run.execute)
                count = len(authority.writes)
                blocked(run.execute)
                assert len(authority.writes) == count
                before = run.database.read_bytes()
                report = run.reconcile()
                assert (
                    run.database.read_bytes() == before
                    and len(authority.writes) == count
                )
                if scenario == "manifest":
                    assert report["state"] == "committed"
                else:
                    assert report["state"] == "resumable"
                runs.record(run.directory, "qualification_state_changed")
                blocked(
                    lambda report=report: run.execute(report["reconciliation_sha256"])
                )
                assert len(authority.writes) == count
                report = run.reconcile()
                unchanged_database = run.database.read_bytes()
                run.database.write_bytes(unchanged_database + b"changed-state")
                blocked(
                    lambda report=report: run.execute(report["reconciliation_sha256"])
                )
                assert len(authority.writes) == count
                run.database.write_bytes(unchanged_database)
                assert (
                    run.execute(report["reconciliation_sha256"])["state"] == "committed"
                )
                if scenario == "manifest":
                    assert len(authority.writes) == count
                assert authority.writes.count("checkpoint") == 1
                assert authority.writes.count("manifest") == 1
            assert run.status(run.directory)["state"] == "committed"
            before = len(authority.writes)
            authority.corrupt_manifest = True
            blocked(run.reconcile)
            assert len(authority.writes) == before
            authority.corrupt_manifest = False
            bad = authority.publication.model_copy(deep=True)
            bad.graphs[0].api_methods[0].index = None
            bad.graphs[0].api_methods[0].error = "remote_result_too_large"
            blocked(lambda bad=bad: run.preflight(run.load(), bad, "bad-method"))
            huge = authority.publication.model_copy(deep=True)
            huge.graphs[0].vendors = ["x" * (2 * 1024 * 1024)]
            blocked(
                lambda huge=huge: run.preflight(run.load(), huge, "stored-row-overflow")
            )
            audit = (run.directory / "audit.jsonl").read_text()
            event_names = [row["event"] for row in runs.events(run.directory)]
            assert event_names.index("preflight") < event_names.index("upload_started")
            assert (
                "synthetic-token" not in audit
                and "synthetic secret" not in audit
                and "Checkpoint evidence" not in audit
            )
            if scenario == "upload":
                assert '"cause_type":"ReadTimeout"' in audit
            assert run.database.stat().st_mode & 0o077 == 0
            assert (run.directory / "prepared.sqlite").exists()
    print(
        "PASS pinned publication: frozen prefixes/discovery, runtime identity, private state, exclusive lock, unknown checkpoint/upload/manifest reconciliation, stale-proof rejection and exact committed manifest"
    )


def main():
    with tempfile.TemporaryDirectory(prefix="ct-preparation-reuse-") as directory:
        root = Path(directory)
        os.environ["HOME"] = str(root / "home")
        journals = root / "journals"
        journals.mkdir()
        os.environ["CT_AMP_LOG_DIR"] = str(journals)
        stamp = "2026-09-10T00:00:00Z"
        for number in (1, 2):
            session = str(UUID(int=number))
            events = [
                {
                    "schema_version": 1,
                    "type": "thread",
                    "captured_at": stamp,
                    "payload": {
                        "id": "T-" + session,
                        "workspace_root": root.as_uri(),
                        "title": "Synthetic reuse",
                    },
                }
            ]
            for role in ("user", "assistant"):
                events.append(
                    {
                        "schema_version": 1,
                        "type": "message",
                        "captured_at": stamp,
                        "thread_id": "T-" + session,
                        "message": {
                            "id": role + "-0",
                            "role": role,
                            "content": [
                                {"type": "text", "text": "Synthetic reuse evidence"}
                            ],
                        },
                    }
                )
            (journals / f"{number}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in events)
            )
        identity = CollectorIdentity(
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            agent_instance_id=UUID(int=3),
            project_name="Reuse",
        )

        def collect():
            with LocalCollector(
                database_path=root / "capture.sqlite3", identity=identity
            ) as collector:
                result = collector.collect(
                    current_dir=root, agent_vendor="amp", heartbeat=False
                )
                assert result.failed == 0 and len(collector.prepared_sources) == 2
                digests = {
                    str(row.session.session_id): canonical_json(
                        row.session.model_dump(mode="json", exclude_none=True)
                    )
                    for row in collector.prepared_sources
                }
                return digests

        first = collect()
        replay = collect()
        assert first == replay

        paths = sorted(journals.glob("*.jsonl"))
        projection_calls = 0
        original_projection = graph_preparation.build_fact_rows

        def measured_projection(graph):
            nonlocal projection_calls
            projection_calls += 1
            return original_projection(graph)

        graph_preparation.build_fact_rows = measured_projection
        store = discover_store_from_files(paths).store
        graphs = list(store.session_graphs.values())
        prepared = [prepare_graph(graph) for graph in graphs]
        assert all(
            value.publication() == build_published_fact_set(graph)
            for graph, value in zip(graphs, prepared, strict=True)
        )
        assert all(len(value.summary.project_sessions) == 1 for value in prepared)
        cache_path = Path.home() / ".coding-trajectory/prepared-graphs.sqlite"
        with sqlite3.connect(cache_path) as db:
            before = db.execute(
                "SELECT key, body FROM prepared ORDER BY key"
            ).fetchall()
        assert len(before) == 2
        assert [prepare_graph(graph) for graph in graphs] == prepared
        assert projection_calls == 2
        with sqlite3.connect(cache_path) as db:
            assert (
                db.execute("SELECT key, body FROM prepared ORDER BY key").fetchall()
                == before
            )
        resolve_calls = 0

        def measured_resolve(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            return discover_store_from_files(paths).store, "qualification"

        repo = LocalPublishedFactRepository(
            global_scope=True,
            current_dir=root,
            cache=IndexCache(),
            resolve=measured_resolve,
        )
        project_id = local_project_id(root, fallback="ignored display name")
        assert project_id == local_project_id(root, fallback="RenamedProject")
        assert project_id != local_project_id(root / "other", fallback="RenamedProject")
        listed = repo.response_for("project.sessions", {"project_id": project_id})
        assert len(listed["items"]) == 2
        assert {item["project_id"] for item in listed["items"]} == {project_id}
        assert (
            repo.response_for("project.sessions", {"project_id": str(UUID(int=999))})[
                "items"
            ]
            == []
        )
        changed = json.loads(paths[0].read_text().splitlines()[-1])
        changed["message"]["id"] = "assistant-1"
        changed["message"]["content"] = [
            {"type": "text", "text": "New unpublished local evidence"}
        ]
        with paths[0].open("a") as stream:
            stream.write(json.dumps(changed) + "\n")
        fresh_graphs = list(
            discover_store_from_files(paths).store.session_graphs.values()
        )
        assert (
            sum(
                graph_input_digest(a) != graph_input_digest(b)
                for a, b in zip(graphs, fresh_graphs, strict=True)
            )
            == 1
        )
        repo.response_for("project.sessions", {"project_id": project_id})
        with sqlite3.connect(cache_path) as db:
            assert db.execute("SELECT COUNT(*) FROM prepared").fetchone()[0] == 3
        index, _ = repo.store_for("graph.stats", {})
        assert (
            sum(
                row.kind == "item"
                for graph_id in index.graph_ids
                for row in index.rows_for_graph(graph_id)
            )
            == 3
        )
        assert projection_calls == 3
        graph_preparation.build_fact_rows = original_projection
        requests = [
            {
                "method": "graph.stats",
                "params": {"root_session_id": str(graph.root_session_id)},
            }
            for graph in graphs
        ]
        before_batch = resolve_calls
        with ServiceRuntime(
            global_scope=True, current_dir=root, historical_repository=repo
        ) as runtime:
            assert all(item["ok"] for item in runtime.batch(requests)["items"])
            assert resolve_calls == before_batch + 1
            assert runtime.execute(requests[0])["ok"]
            assert resolve_calls == before_batch + 2

        # Two real journal locations with the same display name must round-trip
        # independently through the public runtime, not merge under that name.
        other_path = root / "other" / root.name
        rows = [json.loads(line) for line in paths[1].read_text().splitlines()]
        rows[0]["payload"]["workspace_root"] = other_path.as_uri()
        paths[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
        with ServiceRuntime(global_scope=True, current_dir=root) as runtime:
            inventory = runtime.call("project.list", {})["items"]
            assert len(inventory) == 2
            assert len({entry["display_name"] for entry in inventory}) == 1
            for entry in inventory:
                selected_id = entry["project_id"]
                cards = runtime.call("project.sessions", {"project_id": selected_id})[
                    "items"
                ]
                assert len(cards) == 1 and cards[0]["project_id"] == selected_id
            try:
                runtime.call("project.sessions", {"project_name": root.name})
            except ValueError as error:
                assert "ambiguous" in str(error)
            else:
                raise AssertionError("same-name projects were silently merged")

        checkpoint_journals = root / "checkpoint-journals"
        checkpoint_journals.mkdir()
        os.environ["CT_AMP_LOG_DIR"] = str(checkpoint_journals)
        checkpoint_session = str(UUID(int=10))
        checkpoint_events = [
            {
                "schema_version": 1,
                "type": "thread",
                "captured_at": stamp,
                "payload": {
                    "id": "T-" + checkpoint_session,
                    "workspace_root": root.as_uri(),
                    "title": "Synthetic checkpoint",
                },
            },
            {
                "schema_version": 1,
                "type": "message",
                "captured_at": stamp,
                "thread_id": "T-" + checkpoint_session,
                "message": {
                    "id": "assistant-0",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Checkpoint evidence"}],
                },
            },
        ]
        (checkpoint_journals / "checkpoint.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in checkpoint_events)
        )
        remote = CheckpointRemote()
        checkpoint_identity = CollectorIdentity(
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            agent_instance_id=UUID(int=3),
            project_id=UUID(int=4),
            project_name="Checkpoint",
        )
        with LocalCollector(
            database_path=root / "checkpoint.sqlite3", identity=checkpoint_identity
        ) as collector:
            first_checkpoint = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=remote,
                heartbeat=False,
            )
            assert first_checkpoint.failed == 0 and len(remote.requests) == 1
            assert set(remote.requests[0].payload) == {
                "kind",
                "source_checkpoint",
                "session_digest",
            }
            collector._connection.execute(
                "UPDATE logical_sources SET snapshot_schema_version=?",
                ("ct.source_checkpoint.v1:ct-local-collector-v10",),
            )
            collector._connection.commit()
            rollover = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=remote,
                heartbeat=False,
            )
            assert rollover.failed == 0 and len(remote.requests) == 2
            assert remote.registrations[-1].rollover is True
            assert remote.registrations[-1].source_epoch == 2
            assert remote.requests[-1].source_epoch == 2
            assert remote.requests[-1].source_sequence == 0
            assert remote.publications >= 2
            assert collector._connection.execute(
                "SELECT COUNT(*) FROM publication_outbox WHERE state = 'accepted'"
            ).fetchone()[0]
            qualify_upload_scheduling(collector)
            qualify_publication_transport(
                root,
                checkpoint_identity,
                collector._connection.execute(
                    "SELECT * FROM publication_outbox LIMIT 1"
                ).fetchone(),
            )

        # A retired SQL publication must be an explicit local migration stop,
        # never silently discarded, rewritten, or sent to any remote endpoint.
        legacy_path = root / "legacy.sqlite3"
        with LocalCollector(
            database_path=legacy_path, identity=checkpoint_identity
        ) as collector:
            legacy_row = (
                "legacy-key",
                str(checkpoint_identity.project_id),
                7,
                "a" * 64,
                json.dumps({"publication": {"version": 1}, "fact_sets": []}),
                "pending",
                2,
                "legacy retry",
                stamp,
            )
            collector._connection.execute(
                "INSERT INTO publication_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                legacy_row,
            )
            collector._connection.commit()
        calls_before = (
            remote.recoveries,
            len(remote.registrations),
            len(remote.requests),
            remote.uploads,
            remote.publications,
        )
        with LocalCollector(
            database_path=legacy_path, identity=checkpoint_identity
        ) as collector:
            try:
                collector.collect(
                    current_dir=root,
                    agent_vendor="amp",
                    remote=remote,
                    heartbeat=False,
                )
            except RuntimeError as error:
                assert "unsupported_prepared_version" in str(error)
                assert "canonical source" in str(error)
            else:
                raise AssertionError("pending legacy SQL publication was not rejected")
            preserved = tuple(
                collector._connection.execute(
                    "SELECT * FROM publication_outbox WHERE idempotency_key = ?",
                    ("legacy-key",),
                ).fetchone()
            )
            assert preserved == legacy_row
            for state in ("in_flight", "rejected"):
                collector._connection.execute(
                    "UPDATE publication_outbox SET state=?, last_error='conflict'",
                    (state,),
                )
                collector._connection.commit()
                before = tuple(
                    collector._connection.execute(
                        "SELECT * FROM publication_outbox"
                    ).fetchone()
                )
                try:
                    collector.flush(remote)
                except RuntimeError as error:
                    assert "unsupported_prepared_version" in str(error)
                else:
                    raise AssertionError("legacy retry was not rejected")
                assert (
                    tuple(
                        collector._connection.execute(
                            "SELECT * FROM publication_outbox"
                        ).fetchone()
                    )
                    == before
                )
        assert calls_before == (
            remote.recoveries,
            len(remote.registrations),
            len(remote.requests),
            remote.uploads,
            remote.publications,
        )
        qualify_upload_transport()
        qualify_semantic_details(graphs[0], root)
        qualify_pinned_publication(root, checkpoint_journals / "checkpoint.jsonl")
        print(
            json.dumps(
                {
                    "passed": 30,
                    "pinned_publication_scenarios": 8,
                    "batch_resolves_once_then_refreshes": True,
                    "projection_calls_initial_replay_change": [2, 0, 1],
                    "same_name_projects_isolated": True,
                    "shared_preparation": True,
                    "unchanged_cache_entries": 2,
                    "changed_graph_cache_entries": 3,
                    "local_id_selection": True,
                    "deterministic_reparse": first == replay,
                    "checkpoint_requests": len(remote.requests),
                    "clean_rollover": True,
                    "artifact_retry_recovered": True,
                    "legacy_pending_preserved": True,
                    "legacy_pending_remote_calls": 0,
                    "network_requests": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
