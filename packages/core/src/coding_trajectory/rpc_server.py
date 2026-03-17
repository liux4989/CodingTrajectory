"""JSON-RPC 2.0 server over stdin/stdout for coding-trajectory queries."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_trajectory.discovery import DiscoverySource, discover_store, discover_store_from_file, format_discovery_sources
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError
from coding_trajectory.analysis.views import build_trajectory_overview, build_step_details, build_trajectory_scan
from coding_trajectory.service import resolve_collection, resolve_resource, serialize_trajectory_detail

# JSON-RPC / session-api error codes
_ERROR_CODES: dict[str, int] = {
    "trajectory_not_found": 40401,
    "step_not_found": 40404,
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal_error": -32603,
}

_RESOURCE_NOT_FOUND_CODES: dict[str, int] = {
    "trajectory": _ERROR_CODES["trajectory_not_found"],
    "step": _ERROR_CODES["step_not_found"],
}


_CACHE_DIR = Path.home() / ".coding-trajectory"
_CACHE_FILE = _CACHE_DIR / "index.json"


@dataclass
class IndexCache:
    """Lazy index persisted to ~/.coding-trajectory/index.json."""

    path_to_trajectory: dict[str, str] = field(default_factory=dict)
    session_to_trajectory: dict[str, str] = field(default_factory=dict)

    def paths_for_trajectory(self, trajectory_id: str) -> list[str]:
        return [p for p, tid in self.path_to_trajectory.items() if tid == trajectory_id]

    def save(self) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(
                {
                    "path_to_trajectory": self.path_to_trajectory,
                    "session_to_trajectory": self.session_to_trajectory,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> IndexCache:
        if not _CACHE_FILE.exists():
            return cls()
        try:
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        cache = cls(
            path_to_trajectory=raw.get("path_to_trajectory", {}),
            session_to_trajectory=raw.get("session_to_trajectory", {}),
        )
        cache._prune_stale()
        return cache

    def _prune_stale(self) -> None:
        """Remove entries whose source files no longer exist."""
        stale = [p for p in self.path_to_trajectory if not Path(p).exists()]
        if not stale:
            return
        stale_tids = set()
        for p in stale:
            stale_tids.add(self.path_to_trajectory.pop(p))
        live_tids = set(self.path_to_trajectory.values())
        for tid in stale_tids - live_tids:
            self.session_to_trajectory = {
                sid: t for sid, t in self.session_to_trajectory.items() if t != tid
            }


def _error_code_for_resource(message: str) -> int:
    for resource, code in _RESOURCE_NOT_FOUND_CODES.items():
        if resource in message:
            return code
    return _ERROR_CODES["internal_error"]


def _make_response(id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def _make_error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _resolve_trajectory(store: Any, raw_id: str | None) -> Any:
    """Resolve a trajectory by ID, or infer the single trajectory when raw_id is None."""
    if raw_id is not None:
        return resolve_resource(store, "trajectory", raw_id)
    trajectories = list(store.trajectories.values())
    if len(trajectories) == 1:
        return trajectories[0]
    if not trajectories:
        raise ValueError("no trajectories found in store")
    raise ValueError("trajectory_id is required when the store contains multiple trajectories")


def _update_path_index(cache: IndexCache, sources: list[DiscoverySource]) -> None:
    for source in sources:
        if source.trajectory_id is not None:
            cache.path_to_trajectory[str(source.path)] = str(source.trajectory_id)


def _build_store_full(*, global_scope: bool, current_dir: Path, cache: IndexCache) -> tuple[DocumentStore, str]:
    """Full discovery — populates cache.path_to_trajectory."""
    discovery = discover_store(current_dir=current_dir, global_scope=global_scope)
    _update_path_index(cache, discovery.sources)

    return discovery.store, format_discovery_sources(discovery.sources)


def _build_store_targeted(paths: list[str], cache: IndexCache) -> tuple[DocumentStore, str]:
    """Targeted discovery — ingest only the files mapped to a trajectory."""
    from coding_trajectory.discovery import DiscoveryResult

    stores: list[DiscoveryResult] = []
    for p in paths:
        try:
            stores.append(discover_store_from_file(Path(p)))
        except DocumentError:
            continue

    if not stores:
        raise DocumentError(f"no valid log files found for cached paths: {paths}")

    all_trajectories = []
    all_sources = []
    for dr in stores:
        all_trajectories.extend(dr.store.trajectories.values())
        all_sources.extend(dr.sources)

    _update_path_index(cache, all_sources)

    store = DocumentStore.from_trajectories(all_trajectories)
    return store, format_discovery_sources(all_sources)


def _dispatch(
    method: str,
    params: dict[str, Any],
    *,
    store: Any,
    global_scope: bool,
    current_dir: Path,
    discovery_note: str,
    cache: IndexCache,
) -> Any:
    if method == "trajectory.list":
        trajectories = resolve_collection(
            store,
            "trajectory",
            global_scope=global_scope,
            current_dir=current_dir,
            project_name=params.get("project_name"),
            agent_vendor=params.get("agent_vendor"),
        )
        return {
            "items": [serialize_trajectory_detail(t) for t in trajectories],
            "discovery_note": discovery_note,
        }

    if method == "project.list":
        trajectories = resolve_collection(
            store,
            "trajectory",
            global_scope=global_scope,
            current_dir=current_dir,
        )
        names: set[str] = set()
        for t in trajectories:
            if t.project_identifier:
                names.add(t.project_identifier)
        return {
            "items": sorted(names),
            "discovery_note": discovery_note,
        }

    if method == "trajectory.overview":
        trajectory = _resolve_trajectory(store, params.get("trajectory_id"))
        result = build_trajectory_overview(trajectory, store=store)
        for session in trajectory.sessions:
            cache.session_to_trajectory[str(session.session_id)] = str(trajectory.trajectory_id)
        return result

    if method == "step.details":
        step_id = params.get("step_id")
        if not step_id:
            raise ValueError("missing required param: step_id")
        step = resolve_resource(store, "step", step_id)
        return build_step_details(step, store=store)

    if method == "trajectory.scan":
        trajectory = _resolve_trajectory(store, params.get("trajectory_id"))
        step_type = params.get("type")
        if not step_type:
            raise ValueError("missing required param: type")
        filters: list[str] = params.get("filters") or []
        return build_trajectory_scan(trajectory, store=store, step_type=step_type, filters=filters)

    raise KeyError(method)


def _resolve_store(
    params: dict[str, Any],
    *,
    log_file: Path | None,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
) -> tuple[DocumentStore, str]:
    """Build a store: use cached path index for targeted load, fall back to full discovery."""
    if log_file is not None:
        discovery = discover_store_from_file(log_file)
        _update_path_index(cache, discovery.sources)
        return discovery.store, format_discovery_sources(discovery.sources)

    trajectory_id = params.get("trajectory_id")
    if trajectory_id and cache.path_to_trajectory:
        cached_paths = cache.paths_for_trajectory(trajectory_id)
        if cached_paths:
            return _build_store_targeted(cached_paths, cache)

    return _build_store_full(global_scope=global_scope, current_dir=current_dir, cache=cache)


def _handle_request(
    request: dict[str, Any],
    *,
    log_file: Path | None,
    global_scope: bool,
    current_dir: Path,
    cache: IndexCache,
) -> dict[str, Any]:
    req_id = request.get("id")

    method = request.get("method")
    if not isinstance(method, str):
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_request"], "missing or invalid method"))

    params = request.get("params", {})
    if not isinstance(params, dict):
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_params"], "params must be an object"))

    try:
        store, discovery_note = _resolve_store(
            params,
            log_file=log_file,
            global_scope=global_scope,
            current_dir=current_dir,
            cache=cache,
        )
        result = _dispatch(
            method,
            params,
            store=store,
            global_scope=global_scope,
            current_dir=current_dir,
            discovery_note=discovery_note,
            cache=cache,
        )
    except KeyError as exc:
        if str(exc).strip("'\"") == method:
            return _make_response(
                req_id, error=_make_error(_ERROR_CODES["method_not_found"], f"unknown method: {method}"),
            )
        raise
    except ResourceNotFoundError as exc:
        code = _error_code_for_resource(str(exc))
        return _make_response(req_id, error=_make_error(code, str(exc)))
    except DocumentError as exc:
        return _make_response(req_id, error=_make_error(_ERROR_CODES["internal_error"], str(exc)))
    except (ValueError, TypeError) as exc:
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_params"], str(exc)))

    cache.save()
    return _make_response(req_id, result=result)


def serve(argv: list[str] | None = None) -> None:
    """Run the JSON-RPC server loop reading from stdin, writing to stdout."""
    if argv is None:
        argv = sys.argv[1:]

    global_scope = "--global-scope" in argv
    current_dir = Path.cwd()

    log_file: Path | None = None
    if "--log-file" in argv:
        idx = argv.index("--log-file")
        if idx + 1 < len(argv):
            log_file = Path(argv[idx + 1])

    cache = IndexCache.load()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _make_response(None, error=_make_error(_ERROR_CODES["invalid_request"], f"parse error: {exc}"))
        else:
            response = _handle_request(
                request,
                log_file=log_file,
                global_scope=global_scope,
                current_dir=current_dir,
                cache=cache,
            )

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()
