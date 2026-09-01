from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from codex_app_server import CodexAppServerSession
from coding_trajectory.runtime import PluginApiError, default_plugin_client
from pydantic import BaseModel, Field

from datahub_plugin.maintenance.cleanup_metadata import (
    _claude_metadata_paths_by_project,
    _codex_config_paths_by_project,
    _pi_metadata_paths_by_project,
    _remove_codex_project_config_entry,
)

Action = Literal["dry-run", "interactive", "trash", "delete", "cancelled"]


class CleanupTarget(BaseModel):
    path: str
    reason: list[str] = Field(default_factory=list)


class ProjectTarget(CleanupTarget):
    project: str
    cleanup_paths: list[str] = Field(default_factory=list)
    cleanup_config_paths: list[str] = Field(default_factory=list)
    last_activity_at: str | None = None
    session_count: int = 0
    vendors: list[str] = Field(default_factory=list)

    @property
    def display_label(self) -> str:
        return self.project


class SessionTarget(CleanupTarget):
    vendor: str
    modified_at: str | None = None
    session_id: str | None = None

    @property
    def display_label(self) -> str:
        return self.vendor or "session"


type AnyTarget = ProjectTarget | SessionTarget


class SkippedTarget(CleanupTarget):
    kind: str


@dataclass(slots=True)
class CleanupPreview:
    target_kind: Literal["project", "session"]
    candidates: list[AnyTarget]
    skipped: list[SkippedTarget]
    filters: dict[str, Any]


# -------------------------------------------------------------------
# CLI interactive helpers
# -------------------------------------------------------------------


def _print_interactive_candidates(
    candidates: list[AnyTarget],
    *,
    target_kind: str,
) -> None:
    print(f"Cleanup {target_kind}: {len(candidates)} candidate(s)")
    for index, candidate in enumerate(candidates, start=1):
        label = candidate.display_label
        print(f"  c{index:<2} {label}")
        print(f"     {candidate.path}")


def _print_skipped_summary(skipped: list[SkippedTarget]) -> None:
    if not skipped:
        return
    grouped = _skipped_by_reason(skipped)
    print(f"Skipped: {len(skipped)} item(s)")
    for index, (reason, items) in enumerate(grouped.items(), start=1):
        print(f"  s{index:<2} {reason}: {len(items)}")


def _browse_skipped_targets_when_no_candidates(
    skipped: list[SkippedTarget],
    *,
    target_kind: str,
) -> None:
    print(f"Cleanup {target_kind}: 0 candidate(s)")
    _print_skipped_summary(skipped)
    if not skipped:
        return
    raw = input("Inspect skipped items [s=skips, q=close]: ").strip().lower()
    if raw in {"s", "skip", "skips"}:
        _browse_skipped_targets(skipped)


def _browse_skipped_targets(skipped: list[SkippedTarget]) -> None:
    if not skipped:
        print("No skipped items.")
        return
    grouped = _skipped_by_reason(skipped)
    categories = list(grouped.items())
    while True:
        print("Skipped categories:")
        for index, (reason, items) in enumerate(categories, start=1):
            print(f"  s{index:<2} {reason}: {len(items)}")
        raw = input("Expand skipped category [sN/number, b/q=back]: ").strip().lower()
        if raw in {"", "b", "back", "q", "quit", "cancel"}:
            return
        try:
            idx = _parse_prefixed_index(raw, prefix="s")
        except ValueError:
            print("Choose a category number.", file=sys.stderr)
            continue
        if not 1 <= idx <= len(categories):
            print("Choose a category number.", file=sys.stderr)
            continue
        reason, items = categories[idx - 1]
        print(f"Skipped: {reason}")
        for item in sorted(items, key=lambda x: (x.kind, x.path)):
            print(f"  {item.kind}")
            print(f"    {item.path}")


def _skipped_by_reason(
    skipped: list[SkippedTarget],
) -> dict[str, list[SkippedTarget]]:
    grouped: dict[str, list[SkippedTarget]] = {}
    for item in skipped:
        for reason in item.reason or ["unknown"]:
            grouped.setdefault(reason, []).append(item)
    return dict(sorted(grouped.items()))


def _selected_candidates(
    candidates: list[AnyTarget],
    raw_selection: str,
) -> list[AnyTarget]:
    indexes: set[int] = set()
    for part in re.split(r"[\s,]+", raw_selection):
        if not part:
            continue
        try:
            index = _parse_prefixed_index(part, prefix="c")
        except ValueError:
            continue
        if 1 <= index <= len(candidates):
            indexes.add(index)
    return [c for i, c in enumerate(candidates, start=1) if i in indexes]


def _parse_prefixed_index(value: str, *, prefix: str) -> int:
    stripped = value.strip().lower()
    stripped = stripped.removeprefix(prefix)
    return int(stripped)


def _run_cli_interactive(
    candidates: list[AnyTarget],
    skipped: list[SkippedTarget],
    target_kind: str,
) -> tuple[str, list[AnyTarget]]:
    if not candidates:
        _browse_skipped_targets_when_no_candidates(skipped, target_kind=target_kind)
        return ("cancelled", [])

    selected: list[AnyTarget] = []
    while not selected:
        _print_interactive_candidates(candidates, target_kind=target_kind)
        _print_skipped_summary(skipped)
        raw_selection = (
            input("Select candidates [cN/numbers, a=all, s=skips, q=cancel]: ")
            .strip()
            .lower()
        )
        if raw_selection in {"", "q", "quit", "cancel"}:
            return ("cancelled", [])
        if raw_selection in {"s", "skip", "skips"}:
            _browse_skipped_targets(skipped)
            continue
        if raw_selection == "a":
            selected = list(candidates)
        else:
            selected = _selected_candidates(candidates, raw_selection)
        if not selected:
            print("No candidates selected.", file=sys.stderr)

    raw_action = input("Action [t=trash, d=delete, q=cancel]: ").strip().lower()
    if raw_action in {"t", "trash"}:
        result_action = "trash"
    elif raw_action in {"d", "delete"}:
        result_action = "delete"
    else:
        return ("cancelled", [])

    confirmation = (
        input(f"Type {result_action} to confirm {len(selected)} item(s): ")
        .strip()
        .lower()
    )
    if confirmation != result_action:
        return ("cancelled", [])
    return (result_action, selected)


# ---------------------------------------------------------------------------
# ct data surface
# ---------------------------------------------------------------------------


def _load_project_list(params: dict[str, Any]) -> dict[str, Any]:
    return _load_api_result("project.list", params)


def _load_project_sessions(params: dict[str, Any]) -> dict[str, Any]:
    return _load_api_result("project.sessions", params)


def _load_api_result(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = default_plugin_client().call(method, params)
    except PluginApiError as exc:
        raise SystemExit(str(exc)) from exc
    if not isinstance(result, dict):
        raise SystemExit(f"ct api call {method} returned a non-object result")
    return result


def _visible_session_ids(vendor_filter: str | None) -> set[str]:
    params: dict[str, Any] = {}
    if vendor_filter:
        params["agent_vendor"] = vendor_filter
    result = _load_project_sessions(params)

    session_ids: set[str] = set()
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        root_session_id = item.get("root_session_id")
        if isinstance(root_session_id, str) and root_session_id:
            session_ids.add(root_session_id)
        for session_id in item.get("session_ids") or []:
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
    return session_ids


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def preview_project_cleanup(args: argparse.Namespace) -> CleanupPreview:
    older_than = (
        args.older_than
        if isinstance(args.older_than, timedelta)
        else _parse_age(args.older_than)
    )
    root = _cleanup_root(args.path) if args.path else None
    cutoff = datetime.now(UTC) - older_than
    all_projects = _load_project_list({})
    since_days = max(1, round(older_than.total_seconds() / 86_400))
    recent_projects = _load_project_list({"since_days": since_days})
    recent_keys = set((recent_projects.get("items") or {}).keys())
    codex_config_paths_by_project = _codex_config_paths_by_project()
    claude_metadata_paths_by_project = _claude_metadata_paths_by_project()
    pi_metadata_paths_by_project = _pi_metadata_paths_by_project()

    candidates: list[ProjectTarget] = []
    skipped: list[SkippedTarget] = []
    for project_name, item in (all_projects.get("items") or {}).items():
        metadata_paths = [
            *claude_metadata_paths_by_project.get(project_name, []),
            *pi_metadata_paths_by_project.get(project_name, []),
        ]
        target, skip = _project_metadata_target(
            project_name,
            item,
            root=root,
            is_recent=project_name in recent_keys,
            codex_config_paths=codex_config_paths_by_project.get(project_name, []),
            metadata_paths=metadata_paths,
        )
        if target is not None:
            candidates.append(target)
        skipped.extend(skip)

    return CleanupPreview(
        target_kind="project",
        candidates=sorted(candidates, key=lambda item: item.path),
        skipped=_dedupe_skips(skipped),
        filters={
            "older_than": _format_timedelta(older_than),
            "cutoff": cutoff.isoformat(),
            "path": str(root) if root else None,
        },
    )


def handle_project(args: argparse.Namespace) -> dict[str, Any]:
    preview = preview_project_cleanup(args)
    action: Action = "dry-run" if getattr(args, "dry_run", False) else "delete"
    return apply_project_selection(
        args,
        preview,
        action,
        [target for target in preview.candidates if isinstance(target, ProjectTarget)],
    )


def apply_project_selection(
    args: argparse.Namespace,
    preview: CleanupPreview,
    action: Action,
    selected: list[ProjectTarget],
) -> dict[str, Any]:
    manifest_path, action_errors = _apply_action(
        action,
        [
            Path(cleanup_path)
            for target in selected
            for cleanup_path in _target_cleanup_paths(target)
        ],
        target_kind="project",
        config_entries=[
            (Path(config_path), target.path)
            for target in selected
            for config_path in target.cleanup_config_paths
        ],
    )
    return _payload(
        command="cleanup project",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(preview.candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(preview.skipped, key=lambda item: (item.kind, item.path))
        ],
        manifest_path=manifest_path,
        filters=preview.filters,
        discovery_note=None,
        action_errors=action_errors,
    )


def preview_session_cleanup(args: argparse.Namespace) -> CleanupPreview:
    vendor_filter = _normalize_vendor(args.agent_vendor)
    visible_session_ids = _visible_session_ids(vendor_filter)
    candidates: list[SessionTarget] = []
    skipped: list[SkippedTarget] = []
    for vendor, base_dir in _session_sources(vendor_filter):
        if not base_dir.is_dir():
            skipped.append(
                SkippedTarget(
                    kind="session",
                    path=str(base_dir),
                    reason=["missing_session_directory"],
                )
            )
            continue
        for path in sorted(base_dir.rglob("*.jsonl")):
            target, skip = _session_target(
                path,
                vendor=vendor,
                visible_session_ids=visible_session_ids,
            )
            if target is not None:
                candidates.append(target)
            if skip is not None:
                skipped.append(skip)

    return CleanupPreview(
        target_kind="session",
        candidates=sorted(candidates, key=lambda item: item.path),
        skipped=_dedupe_skips(skipped),
        filters={"agent_vendor": vendor_filter},
    )


def handle_session(args: argparse.Namespace) -> dict[str, Any]:
    preview = preview_session_cleanup(args)
    action = _resolve_action(args)
    action, selected = _resolve_interactive_selection(
        action,
        preview.candidates,
        skipped=preview.skipped,
        target_kind=preview.target_kind,
    )
    return apply_session_selection(
        args,
        preview,
        action,
        [target for target in selected if isinstance(target, SessionTarget)],
    )


def apply_session_selection(
    args: argparse.Namespace,
    preview: CleanupPreview,
    action: Action,
    selected: list[SessionTarget],
) -> dict[str, Any]:
    manifest_path, action_errors = _apply_session_action(action, selected)
    return _payload(
        command="cleanup session",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(preview.candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(preview.skipped, key=lambda item: (item.kind, item.path))
        ],
        manifest_path=manifest_path,
        filters=preview.filters,
        discovery_note=None,
        action_errors=action_errors,
    )


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _project_metadata_target(
    project_name: str,
    item: dict[str, Any],
    *,
    root: Path | None,
    is_recent: bool,
    codex_config_paths: list[Path],
    metadata_paths: list[Path],
) -> tuple[ProjectTarget | None, list[SkippedTarget]]:
    raw_path = item.get("path")
    project_path = (
        Path(raw_path).expanduser() if isinstance(raw_path, str) and raw_path else None
    )
    if project_path is None:
        return None, [_skip("project", project_name, "missing_project_path")]
    try:
        resolved = project_path.resolve()
    except OSError:
        resolved = project_path

    reasons: list[str] = []
    skips: list[SkippedTarget] = []
    if root is not None and not _is_relative_to(resolved, root):
        skips.append(_skip("project", str(resolved), "outside_cleanup_root"))
    if _is_current_or_parent(resolved, Path.cwd().resolve()):
        skips.append(_skip("project", str(resolved), "current_directory_or_parent"))

    if not resolved.exists():
        reasons.append("project_path_missing")
    else:
        if is_recent:
            skips.append(_skip("project", str(resolved), "newer_than_retention"))
        else:
            reasons.append("older_than_retention")
        if not _looks_like_project_directory(resolved):
            skips.append(_skip("project", str(resolved), "not_project_directory"))
        if (resolved / ".ct-cleanup-keep").exists():
            skips.append(_skip("project", str(resolved), "keep_marker"))
        git_skips = _git_skip_reasons(resolved)
        skips.extend(_skip("project", str(resolved), reason) for reason in git_skips)

    if skips:
        return None, skips

    cleanup_config_paths = (
        [str(path) for path in codex_config_paths] if not resolved.exists() else []
    )
    cleanup_paths = (
        [str(path) for path in metadata_paths] if not resolved.exists() else []
    )
    if resolved.exists():
        cleanup_paths = [str(resolved)]
        reasons.append("project_directory_deletion")
    elif not cleanup_paths and not cleanup_config_paths:
        return None, [_skip("project", str(resolved), "no_cleanup_paths")]
    return (
        ProjectTarget(
            project=project_name,
            path=str(resolved),
            cleanup_paths=cleanup_paths,
            cleanup_config_paths=cleanup_config_paths,
            reason=reasons or ["old_project"],
            last_activity_at=None,
            session_count=len(metadata_paths) if cleanup_paths else 0,
            vendors=sorted(item.get("vendors") or []),
        ),
        [],
    )


def _session_target(
    path: Path,
    *,
    vendor: str,
    visible_session_ids: set[str],
) -> tuple[SessionTarget | None, SkippedTarget | None]:
    records = _load_jsonl(path)
    if records is None:
        return None, _skip("session", str(path), "invalid_session_log")
    session_id = _session_id_from_records(vendor, records)
    if not session_id:
        return None, _skip("session", str(path), "missing_session_id")
    if session_id in visible_session_ids:
        if _recently_modified(path, timedelta(hours=24)):
            return None, _skip("session", str(path), "active_session_log")
        return None, _skip("session", str(path), "visible_session")

    reasons = ["orphan_session_log"]
    if _recently_modified(path, timedelta(hours=24)):
        reasons.append("recent_unlisted_session_log")

    return (
        SessionTarget(
            vendor=vendor,
            path=str(path.resolve()),
            reason=reasons,
            modified_at=_modified_at(path),
            session_id=session_id,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Codex config cleanup
# ---------------------------------------------------------------------------


def _payload(
    *,
    command: str,
    action: Action,
    targets: list[dict[str, Any]],
    candidate_count: int,
    skipped: list[dict[str, Any]],
    manifest_path: str | None,
    filters: dict[str, Any],
    discovery_note: str | None,
    action_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "command": command,
        "action": action,
        "generated_at": datetime.now(UTC).isoformat(),
        "filters": filters,
        "summary": {
            "target_count": len(targets),
            "candidate_count": candidate_count,
            "skipped_count": len(skipped),
            "error_count": len(action_errors),
            "skipped_reasons": dict(
                Counter(reason for item in skipped for reason in item.get("reason", []))
            ),
        },
        "targets": targets,
        "skipped": skipped,
        "manifest_path": manifest_path,
        "discovery_note": discovery_note,
        "errors": action_errors,
    }


def _dedupe_skips(skipped: list[SkippedTarget]) -> list[SkippedTarget]:
    merged: dict[tuple[str, str], set[str]] = {}
    for item in skipped:
        key = (item.kind, item.path)
        merged.setdefault(key, set()).update(item.reason)
    return [
        SkippedTarget(
            kind=kind,
            path=path,
            reason=sorted(reasons),
        )
        for (kind, path), reasons in merged.items()
    ]


# ---------------------------------------------------------------------------
# Actions and selection
# ---------------------------------------------------------------------------


def _resolve_action(args: argparse.Namespace) -> Action:
    if getattr(args, "dry_run", False):
        action: Action = "dry-run"
    elif getattr(args, "delete", False):
        action: Action = "delete"
    elif getattr(args, "trash", False):
        action = "trash"
    else:
        return "interactive"
    if action == "dry-run":
        return action
    if not getattr(args, "confirm", False):
        raise ValueError(f"--{action} requires --confirm")
    return action


def _resolve_interactive_selection(
    action: Action,
    candidates: list[ProjectTarget | SessionTarget],
    *,
    skipped: list[SkippedTarget],
    target_kind: str,
) -> tuple[Action, list[ProjectTarget | SessionTarget]]:
    if action != "interactive":
        return action, list(candidates)
    return _run_cli_interactive(list(candidates), skipped, target_kind)


def _target_cleanup_paths(target: ProjectTarget | SessionTarget) -> list[str]:
    if not isinstance(target, ProjectTarget):
        return [target.path]
    return target.cleanup_paths


def _apply_action(
    action: Action,
    paths: list[Path],
    *,
    target_kind: str,
    config_entries: list[tuple[Path, str]] | None = None,
) -> tuple[str | None, list[dict[str, str]]]:
    config_entries = config_entries or []
    if action in {"dry-run", "interactive", "cancelled"} or (
        not paths and not config_entries
    ):
        return None, []
    manifest = {
        "action": action,
        "target_kind": target_kind,
        "generated_at": datetime.now(UTC).isoformat(),
        "paths": [str(path) for path in paths],
        "config_entries": [
            {"config_path": str(path), "project_path": project_path}
            for path, project_path in config_entries
        ],
        "errors": [],
    }
    errors: list[dict[str, str]] = []
    for config_path, project_path in config_entries:
        try:
            _remove_codex_project_config_entry(config_path, project_path)
        except OSError as exc:
            errors.append({"path": str(config_path), "error": str(exc)})
    for path in paths:
        try:
            if action == "trash":
                _move_to_trash(path)
            elif action == "delete":
                _delete_path(path)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    manifest["errors"] = errors
    manifest_path = _write_manifest(manifest)
    return str(manifest_path), errors


def _apply_session_action(
    action: Action,
    selected: list[SessionTarget],
) -> tuple[str | None, list[dict[str, str]]]:
    if action in {"dry-run", "interactive", "cancelled"} or not selected:
        return None, []

    codex_targets = [
        target
        for target in selected
        if target.vendor == "codex_cli" and action == "delete"
    ]
    filesystem_paths = [
        Path(target.path)
        for target in selected
        if not (target.vendor == "codex_cli" and action == "delete")
    ]
    manifest = {
        "action": action,
        "target_kind": "session",
        "generated_at": datetime.now(UTC).isoformat(),
        "paths": [str(path) for path in filesystem_paths],
        "codex_threads": [
            {
                "thread_id": target.session_id,
                "path": target.path,
            }
            for target in codex_targets
        ],
        "config_entries": [],
        "errors": [],
    }
    errors: list[dict[str, str]] = []

    if codex_targets:
        codex_errors = _delete_codex_session_threads(codex_targets)
        errors.extend(codex_errors)
        failed_codex_paths = {error["path"] for error in codex_errors}
        filesystem_paths.extend(
            Path(target.path)
            for target in codex_targets
            if target.path not in failed_codex_paths
        )

    for path in filesystem_paths:
        try:
            if action == "trash":
                _move_to_trash(path)
            elif action == "delete":
                _delete_path(path)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})

    manifest["errors"] = errors
    manifest_path = _write_manifest(manifest)
    return str(manifest_path), errors


def _delete_codex_session_threads(
    targets: list[SessionTarget],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    session: CodexAppServerSession | None = None
    try:
        session = CodexAppServerSession(cwd=Path.cwd(), timeout_seconds=30)
        for target in targets:
            if not target.session_id:
                errors.append(
                    {
                        "path": target.path,
                        "error": "missing Codex thread id",
                    }
                )
                continue
            try:
                session.delete_thread(target.session_id)
            except RuntimeError as exc:
                errors.append(
                    {
                        "path": target.path,
                        "error": str(exc),
                    }
                )
    except RuntimeError as exc:
        return [
            {
                "path": target.path,
                "error": str(exc),
            }
            for target in targets
        ]
    finally:
        if session is not None:
            session.close()
    return errors


def _write_manifest(manifest: dict[str, Any]) -> Path:
    directory = Path.home() / ".coding-trajectory" / "cleanup-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"cleanup-{stamp}.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _move_to_trash(path: Path) -> None:
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    destination = _unique_destination(trash / path.name)
    shutil.move(str(path), str(destination))


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not find unique trash destination for {path}")


# ---------------------------------------------------------------------------
# Session sources
# ---------------------------------------------------------------------------


def _session_sources(vendor_filter: str | None) -> list[tuple[str, Path]]:
    home = Path.home()
    sources: list[tuple[str, Path]] = [
        ("codex_cli", home / ".codex" / "sessions"),
        ("pi", home / ".pi" / "agent" / "sessions"),
    ]
    if vendor_filter is None:
        return sources
    return [source for source in sources if source[0] == vendor_filter]


def _normalize_vendor(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    aliases = {"codex": "codex_cli", "codex-cli": "codex_cli"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"codex_cli", "pi"}:
        raise ValueError(
            "unknown --agent-vendor value; expected codex_cli, codex, or pi"
        )
    return normalized


# ---------------------------------------------------------------------------
# Time / age helpers
# ---------------------------------------------------------------------------


def _parse_age(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([dh])\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("must look like 30d or 12h")
    amount = int(match.group(1))
    if amount < 1:
        raise argparse.ArgumentTypeError("must be positive")
    unit = match.group(2)
    return timedelta(days=amount) if unit == "d" else timedelta(hours=amount)


def _format_timedelta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    return f"{seconds}s"


def _cleanup_root(raw_path: str | None) -> Path:
    return Path(raw_path).expanduser().resolve()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_skip_reasons(path: Path) -> list[str]:
    if not (path / ".git").exists():
        return []
    reasons: list[str] = []
    status = _git(path, "status", "--porcelain")
    if status is None:
        reasons.append("git_status_failed")
    elif status.strip():
        reasons.append("git_dirty")
    unpushed = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if unpushed is None:
        reasons.append("git_upstream_missing")
    else:
        try:
            if int(unpushed.strip() or "0") > 0:
                reasons.append("git_unpushed_commits")
        except ValueError:
            reasons.append("git_unpushed_check_failed")
    return reasons


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _session_id_from_records(vendor: str, records: list[dict[str, Any]]) -> str | None:
    if vendor == "codex_cli":
        for record in records:
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                return payload["id"]
    if vendor == "pi":
        for record in records:
            if record.get("type") == "session" and isinstance(record.get("id"), str):
                return record["id"]
    return None


def _load_jsonl(path: Path) -> list[dict[str, Any]] | None:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, json.JSONDecodeError):
        return None
    return records


# ---------------------------------------------------------------------------
# File system helpers
# ---------------------------------------------------------------------------


def _recently_modified(path: Path, duration: timedelta) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    return modified >= datetime.now(UTC) - duration


def _modified_at(path: Path) -> str | None:
    modified_at = _path_modified_at_datetime(path)
    return modified_at.isoformat() if modified_at is not None else None


def _path_modified_at_datetime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_current_or_parent(path: Path, current: Path) -> bool:
    return path == current or path in current.parents


def _looks_like_project_directory(path: Path) -> bool:
    if not path.is_dir():
        return False
    project_markers = {
        ".git",
        ".hg",
        ".svn",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "settings.gradle",
        "Makefile",
        "README.md",
    }
    return any((path / marker).exists() for marker in project_markers)


def _skip(kind: str, path: str, reason: str) -> SkippedTarget:
    return SkippedTarget(kind=kind, path=path, reason=[reason])


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "detail", False):
        return json.dumps(payload, indent=2, ensure_ascii=False)
    summary = payload.get("summary") or {}
    lines = [
        payload.get("command", "cleanup"),
        f"Action: {payload.get('action') or 'interactive'}",
        f"Selected: {summary.get('target_count') or 0} of {summary.get('candidate_count') or 0}",
        f"Skipped: {summary.get('skipped_count') or 0}",
    ]
    if summary.get("error_count"):
        lines.append(f"Errors: {summary['error_count']}")
    skipped_reasons = summary.get("skipped_reasons") or {}
    if skipped_reasons:
        lines.append("Skip reasons:")
        for reason, count in sorted(skipped_reasons.items()):
            lines.append(f"  {reason}: {count}")
    targets = payload.get("targets") or []
    if targets:
        lines.append("Candidates:")
        for item in targets[:20]:
            details = []
            if item.get("project"):
                details.append(str(item["project"]))
            if item.get("vendor"):
                details.append(str(item["vendor"]))
            if details:
                lines.append("  " + "  ".join(details))
            lines.append(f"    {item.get('path')}")
        if len(targets) > 20:
            lines.append(f"  ... {len(targets) - 20} more.")
    if payload.get("manifest_path"):
        lines.append(f"Manifest: {payload['manifest_path']}")
    errors = payload.get("errors") or []
    if errors:
        lines.append("Action errors:")
        for item in errors[:10]:
            lines.append(f"  {item.get('path')}: {item.get('error')}")
    return "\n".join(lines).rstrip()
