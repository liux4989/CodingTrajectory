"""Collector identity, recovery, and checkpoint operations."""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from shared import State, receipt, require_that, stable, text

Json = dict[str, Any]


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def register_project(state: State, request: Json) -> Json:
    name = text(request["display_name"]).strip()
    require_that(
        bool(name) and "/" not in name and "\\" not in name,
        "portable_project_name_required",
    )
    repository = request.get("repository_identity")
    aliases = sorted(
        {
            alias.strip()
            for alias in request.get("aliases", [])
            if alias.strip() and alias.strip().lower() != name.lower()
        }
    )
    projects = state.all("project")
    existing = next(
        (
            project
            for project in projects
            if repository and project.get("repository_identity") == repository
        ),
        None,
    ) or next(
        (
            project
            for project in projects
            if project["display_name"].lower() == name.lower()
        ),
        None,
    )
    if (
        existing
        and existing["display_name"] == name
        and existing.get("repository_identity") == repository
        and stable(existing.get("aliases", [])) == stable(aliases)
    ):
        return {
            "project_id": existing["project_id"],
            "revision": existing["revision"],
            "committed_sequence": existing["published_sequence"],
        }
    project_id = existing["project_id"] if existing else str(uuid.uuid4())
    sequence = state.next()
    revision = (existing["revision"] if existing else 0) + 1
    state.put(
        "project",
        project_id,
        {
            "project_id": project_id,
            "revision": revision,
            "display_name": name,
            "repository_identity": repository,
            "aliases": aliases,
            "published_sequence": sequence,
            "modified_at": _now_iso(),
        },
        sequence,
    )
    return {
        "project_id": project_id,
        "revision": revision,
        "committed_sequence": sequence,
    }


def register_source(state: State, request: Json) -> Json:
    if request.get("project_id"):
        require_that(
            state.get("project", request["project_id"]), "project_not_found", 404
        )
    identity = stable(
        [request["agent_id"], request["vendor"], request["native_session_id"]]
    )
    existing = state.get("source_identity", identity)
    source = state.get("source", existing["source_id"]) if existing else None
    epoch = request.get("source_epoch", 1)
    if source:
        require_that(
            source.get("project_id") == request.get("project_id"),
            "source_project_conflict",
            409,
        )
        require_that(
            epoch == source["source_epoch"]
            or (request.get("rollover") and epoch == source["source_epoch"] + 1),
            "source_epoch_conflict",
            409,
        )
        if epoch != source["source_epoch"]:
            state.put(
                "source",
                source["source_id"],
                {**source, "source_epoch": epoch, "committed_source_sequence": -1},
                state.next(),
            )
        return {"source_id": source["source_id"], "source_epoch": epoch}
    require_that(epoch == 1, "new_source_epoch_must_be_one")
    source_id = str(uuid.uuid4())
    sequence = state.next()
    state.put("source_identity", identity, {"source_id": source_id}, sequence)
    state.put(
        "source",
        source_id,
        {
            "source_id": source_id,
            "agent_id": request["agent_id"],
            "project_id": request.get("project_id"),
            "source_epoch": 1,
            "vendor": request["vendor"],
            "native_session_id": request["native_session_id"],
            "committed_source_sequence": -1,
        },
        sequence,
    )
    return {"source_id": source_id, "source_epoch": 1}


def recovery(state: State, request: Json) -> Json:
    publisher = state.get("artifact_project_publisher", request["project_id"])
    source = None
    if request.get("vendor") and request.get("native_session_id"):
        identity = state.get(
            "source_identity",
            stable(
                [request["agent_id"], request["vendor"], request["native_session_id"]]
            ),
        )
        current = state.get("source", identity["source_id"]) if identity else None
        if current:
            require_that(
                current.get("project_id") == request["project_id"],
                "source_project_conflict",
                409,
            )
            checkpoint_record = state.get(
                "checkpoint",
                f"{current['source_id']}:{current['source_epoch']}:{current['committed_source_sequence']}",
            )
            source = {
                "source_id": current["source_id"],
                "source_epoch": current["source_epoch"],
                "next_source_sequence": current["committed_source_sequence"] + 1,
                "content_sha256": checkpoint_record.get("content_sha256")
                if checkpoint_record
                else None,
            }
    living = (
        state.get("living_head", request["agent_instance_id"])
        if request.get("agent_instance_id")
        else None
    )
    if living:
        require_that(
            living["agent_id"] == request["agent_id"], "agent_instance_conflict", 403
        )
    publication_receipt = None
    if request.get("publication_idempotency_key"):
        prior = state.get(
            "receipt",
            stable(
                [
                    request["agent_id"],
                    "ct_collector_publish_artifacts",
                    request["publication_idempotency_key"],
                ]
            ),
        )
        publication_receipt = prior.get("result") if prior else None
    return {
        "next_publication_sequence": (
            publisher.get("publication_sequence", -1) if publisher else -1
        )
        + 1,
        "next_living_sequence": (
            (living.get("observation_sequence", 0) if living else 0) + 1
        )
        if request.get("agent_instance_id")
        else None,
        "source": source,
        "authority_sequence": state.head(),
        "publication_receipt": publication_receipt,
    }


def checkpoint(state: State, request: Json) -> Json:
    source = state.get("source", request["source_id"])
    require_that(
        source and source["agent_id"] == request["agent_id"], "source_agent_denied", 403
    )
    require_that(
        source["source_epoch"] == request["source_epoch"], "source_epoch_conflict", 409
    )
    key = (
        f"{request['source_id']}:{request['source_epoch']}:{request['source_sequence']}"
    )
    event_key = (
        f"{request['source_id']}:{request['source_epoch']}:{request['event_id']}"
    )
    existing = state.get("checkpoint", key) or state.get("checkpoint_event", event_key)
    if existing:
        duplicate = (
            existing["event_id"] == request["event_id"]
            and existing["source_sequence"] == request["source_sequence"]
            and existing["content_sha256"] == request["content_sha256"]
        )
        return receipt(
            "duplicate" if duplicate else "conflict",
            None,
            {
                "reason": "event_identity_already_accepted"
                if duplicate
                else "event_identity_or_sequence_reused_with_different_content"
            },
        )
    sequence = state.next()
    state.put("checkpoint", key, request, sequence)
    state.put("checkpoint_event", event_key, request, sequence)
    contiguous = source["committed_source_sequence"]
    while state.get(
        "checkpoint", f"{source['source_id']}:{source['source_epoch']}:{contiguous + 1}"
    ):
        contiguous += 1
    state.put(
        "source",
        source["source_id"],
        {**source, "committed_source_sequence": contiguous},
        sequence,
    )
    return receipt("accepted", sequence)
