"""Synchronize stored local projections before reading canonical remote results."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from coding_trajectory.control_plane.collector import (
    CollectorIdentity,
    CollectorRemoteError,
    LocalCollector,
    SupabaseCollectorRemote,
)
from coding_trajectory.control_plane.collector_protocol import (
    ProjectRegistrationRequest,
)
from coding_trajectory.control_plane.publication_lock import publication_lock
from coding_trajectory.control_plane.shareable import build_shareable_graph_artifact
from coding_trajectory.discovery import locate_session_files
from coding_trajectory.query import DocumentError


class PublicationState(BaseModel):
    pending_session: str | None = None
    fingerprints: dict[str, str] = Field(default_factory=dict)
    digests: dict[str, str] = Field(default_factory=dict)


class OnDemandPublisher:
    """Store-first origin-host capability, never available to HTTP handlers."""

    def __init__(
        self,
        *,
        factory: Any,
        access_token: str,
        agent_id: UUID,
        url: str,
        api_key: str,
        current_dir: Path,
        project_id: UUID | None = None,
    ):
        self.factory = factory
        self.access_token = access_token
        self.agent_id = agent_id
        self.current_dir = current_dir.resolve()
        self.project_id = project_id
        self.remote = SupabaseCollectorRemote(
            url=url, api_key=api_key, access_token=access_token
        )
        key = hashlib.sha256(
            f"{factory.workspace_id}:{agent_id}:{self.current_dir.name}".encode()
        ).hexdigest()
        root = Path(
            os.environ.get(
                "CT_ON_DEMAND_STATE_DIR", "~/.coding-trajectory/control-plane/on-demand"
            )
        ).expanduser()
        self.directory = root / key

    def _options(self) -> dict[str, Any]:
        return self.factory.runtime_options(
            self.access_token, local_evidence=True, current_dir=self.current_dir
        )

    @staticmethod
    def _canonical(repository: Any) -> Any:
        return getattr(repository, "canonical", repository)

    @staticmethod
    def _read(repository: Any, method: str, params: dict[str, Any]):
        if method in {"session.events", "session.search"}:
            method = "session.overview"
            params = {
                key: params[key]
                for key in ("session_id", "root_session_id", "turn_id")
                if params.get(key)
            }
        else:
            params = dict(params)
            if method == "session.items":
                params["include_content"] = False
            if method == "graph.overview":
                params["include"] = []
        return OnDemandPublisher._canonical(repository).store_for(method, params)[0]

    def _save(self, state: PublicationState) -> None:
        temporary = self.directory / "state.tmp"
        temporary.write_text(state.model_dump_json())
        temporary.replace(self.directory / "state.json")

    def _prepare_living(self, method: str) -> dict[str, Any]:
        from coding_trajectory.contracts import service_contract
        from coding_trajectory.living_events import serve_living_events
        from coding_trajectory.living_sessions import serve_living_sessions
        from coding_trajectory.service import IndexCache

        with publication_lock(self.factory.workspace_id, self.agent_id):
            self.directory.mkdir(parents=True, exist_ok=True)
            registration = self.remote.register_project(
                ProjectRegistrationRequest(
                    workspace_id=self.factory.workspace_id,
                    agent_id=self.agent_id,
                    display_name=self.current_dir.name,
                )
            )
            if (
                self.project_id is not None
                and self.project_id != registration.project_id
            ):
                raise DocumentError(
                    "local project does not match the collector credential profile"
                )
            identity = CollectorIdentity(
                workspace_id=self.factory.workspace_id,
                agent_id=self.agent_id,
                agent_instance_id=uuid5(
                    NAMESPACE_URL,
                    f"ct-on-demand:{self.factory.workspace_id}:{self.agent_id}",
                ),
                project_id=registration.project_id,
                project_name=self.current_dir.name,
            )
            with LocalCollector(
                database_path=self.directory / "collector.sqlite3",
                identity=identity,
            ) as collector:
                print(
                    f"ct: synchronizing stored {method} observations with Supabase…",
                    file=sys.stderr,
                    flush=True,
                )
                cache = IndexCache()

                def load_page(params: dict[str, Any]) -> dict[str, Any]:
                    if method == "living.events":
                        response = serve_living_events(
                            params,
                            cache=cache,
                            current_dir=self.current_dir,
                            global_scope=False,
                        )
                    else:
                        response = serve_living_sessions(
                            params,
                            current_dir=self.current_dir,
                            global_scope=False,
                        )
                    return service_contract(method).validate_response(response)

                collector.publish_living_projection(
                    remote=self.remote,
                    kind=method,
                    load_page=load_page,
                )
                collector.refresh_lease(self.remote)
                if collector.pending_count():
                    raise DocumentError(
                        "living publication is incomplete; retry this request"
                    )
        return self._options()

    def prepare(
        self, method: str, params: dict[str, Any], repository: Any
    ) -> dict[str, Any] | None:
        if method in {"living.events", "living.sessions"}:
            try:
                return self._prepare_living(method)
            except DocumentError:
                raise
            except (CollectorRemoteError, OSError, TypeError, ValueError) as exc:
                raise DocumentError(
                    f"on-demand living publication failed ({type(exc).__name__}); retry this request"
                ) from None
        if not method.startswith(("session.", "graph.")):
            return None
        target = params.get("session_id") or params.get("root_session_id")
        if not target:
            return None
        session_id = UUID(target)
        # An authorization/network failure is never interpreted as a cache miss.
        published = self._read(repository, method, params)
        paths = locate_session_files(
            session_id=session_id,
            current_dir=self.current_dir,
            global_scope=False,
            since_days=7,
            include_descendants=True,
        )
        if not paths:
            return None
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    (
                        str(path),
                        (stat := path.stat()).st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                    for path in sorted(paths)
                ]
            ).encode()
        ).hexdigest()
        try:
            with publication_lock(self.factory.workspace_id, self.agent_id):
                self.directory.mkdir(parents=True, exist_ok=True)
                state_path = self.directory / "state.json"
                state = (
                    PublicationState.model_validate_json(state_path.read_text())
                    if state_path.exists()
                    else PublicationState()
                )
                if state.pending_session and state.pending_session != target:
                    raise DocumentError(
                        "another session has a pending publication; retry its query first"
                    )
                if (
                    not state.pending_session
                    and state.fingerprints.get(target) == fingerprint
                    and session_id in published.sessions
                    and state.digests.get(target)
                    in {
                        build_shareable_graph_artifact(graph).digest()
                        for graph in published.session_graphs.values()
                    }
                ):
                    return None
                # A concurrent caller may have committed while this caller waited.
                options = self._options()
                latest = self._read(options["historical_repository"], method, params)
                if (
                    not state.pending_session
                    and state.fingerprints.get(target) == fingerprint
                    and session_id in latest.sessions
                    and state.digests.get(target)
                    in {
                        build_shareable_graph_artifact(graph).digest()
                        for graph in latest.session_graphs.values()
                    }
                ):
                    return options
                registration = self.remote.register_project(
                    ProjectRegistrationRequest(
                        workspace_id=self.factory.workspace_id,
                        agent_id=self.agent_id,
                        display_name=self.current_dir.name,
                    )
                )
                if (
                    self.project_id is not None
                    and self.project_id != registration.project_id
                ):
                    raise DocumentError(
                        "local project does not match the collector credential profile"
                    )
                identity = CollectorIdentity(
                    workspace_id=self.factory.workspace_id,
                    agent_id=self.agent_id,
                    agent_instance_id=uuid5(
                        NAMESPACE_URL,
                        f"ct-on-demand:{self.factory.workspace_id}:{self.agent_id}",
                    ),
                    project_id=registration.project_id,
                    project_name=self.current_dir.name,
                )
                state.pending_session = target
                self._save(state)
                print(
                    "ct: synchronizing the requested session with Supabase…",
                    file=sys.stderr,
                    flush=True,
                )
                with LocalCollector(
                    database_path=self.directory / "collector.sqlite3",
                    identity=identity,
                ) as collector:
                    try:
                        result = collector.collect(
                            current_dir=self.current_dir,
                            since_days=7,
                            remote=self.remote,
                            heartbeat=False,
                            target_session_id=session_id,
                            known_artifact_digests={
                                build_shareable_graph_artifact(graph).digest()
                                for graph in latest.session_graphs.values()
                            },
                        )
                        if (
                            result.failed
                            or result.pending
                            or result.rejected
                            or result.artifacts_rejected
                            or result.artifact_scope_incomplete
                        ):
                            raise DocumentError(
                                "session publication is incomplete; retry this query after resolving the collector error"
                            )
                    except Exception:
                        if not collector.pending_count():
                            state.pending_session = None
                            self._save(state)
                        raise
                if result.artifacts_accepted:
                    options = self._options()
                visible = self._read(options["historical_repository"], method, params)
                if session_id not in visible.sessions:
                    raise DocumentError(
                        "publication completed but the requested session is not visible"
                    )
                if result.target_artifact_digest not in {
                    build_shareable_graph_artifact(graph).digest()
                    for graph in visible.session_graphs.values()
                }:
                    raise DocumentError(
                        "the requested revision has not been published; resolve the pending collector delivery before retrying"
                    )
                state.pending_session = None
                state.fingerprints[target] = fingerprint
                state.digests[target] = result.target_artifact_digest
                self._save(state)
                return options
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(
                f"on-demand publication failed ({type(exc).__name__}); retry this query"
            ) from None
