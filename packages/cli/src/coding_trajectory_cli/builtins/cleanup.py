from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.models import EventType, Session, SessionStatus, Vendor

from coding_trajectory_cli.plugins import CtPluginContext


Action = Literal["interactive", "trash", "delete", "cancelled"]


class CleanupTarget(BaseModel):
    path: str
    bytes: int = 0
    reason: list[str] = Field(default_factory=list)


class ProjectTarget(CleanupTarget):
    project: str
    cleanup_paths: list[str] = Field(default_factory=list)
    last_activity_at: str | None = None
    session_count: int = 0
    vendors: list[str] = Field(default_factory=list)


class SessionTarget(CleanupTarget):
    vendor: str
    modified_at: str | None = None
    session_id: str | None = None


class SkippedTarget(CleanupTarget):
    kind: str


class CleanupPlugin:
    name = "cleanup"

    def register(
        self, namespace_subparsers: argparse._SubParsersAction, ctx: CtPluginContext
    ) -> None:
        cleanup = namespace_subparsers.add_parser(
            "cleanup",
            help="Clean up old project directories and empty session logs.",
        )
        cleanup_sub = cleanup.add_subparsers(dest="cleanup_action", required=True)

        project = cleanup_sub.add_parser(
            "project",
            help="Preview or remove old project directories.",
        )
        project.add_argument(
            "--older-than",
            default="30d",
            type=_parse_age,
            metavar="AGE",
            help="Select projects with no activity newer than AGE. Supports Nd or Nh. Defaults to 30d.",
        )
        project.add_argument(
            "--path",
            default=None,
            help="Only consider project paths under this cleanup root. Defaults to all projects from ct project list.",
        )
        _add_action_flags(project)
        project.set_defaults(_cleanup_handler=lambda args: _handle_project(args, ctx))
        ctx.bind_command(
            project,
            handler=lambda args: args._cleanup_handler(args),
            renderer=_render_cleanup,
        )

        session = cleanup_sub.add_parser(
            "session",
            help="Preview or remove empty session logs.",
        )
        session.add_argument(
            "--agent-vendor",
            default=None,
            help="Filter by vendor. Known values: codex_cli, codex, pi.",
        )
        _add_action_flags(session)
        session.set_defaults(_cleanup_handler=_handle_session)
        ctx.bind_command(
            session,
            handler=lambda args: args._cleanup_handler(args),
            renderer=_render_cleanup,
        )


def _add_action_flags(parser: argparse.ArgumentParser) -> None:
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--trash",
        action="store_true",
        help="Move all selected candidates to the user trash after confirmation.",
    )
    action.add_argument(
        "--delete",
        action="store_true",
        help="Permanently delete all selected candidates after confirmation.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --trash or --delete.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print the structured JSON data instead of the human-readable overview.",
    )


def _handle_project(args: argparse.Namespace, ctx: CtPluginContext) -> dict[str, Any]:
    action = _resolve_action(args)
    root = _cleanup_root(args.path) if args.path else None
    cutoff = datetime.now(timezone.utc) - args.older_than
    all_projects = ctx.dispatch_core(
        method="project.list",
        params={},
        global_scope=True,
        current_dir=Path.cwd(),
    )
    recent_projects = ctx.dispatch_core(
        method="project.list",
        params={"modified_since": cutoff},
        global_scope=True,
        current_dir=Path.cwd(),
    )
    recent_keys = set((recent_projects.get("items") or {}).keys())

    candidates: list[ProjectTarget] = []
    skipped: list[SkippedTarget] = []
    for project_name, item in (all_projects.get("items") or {}).items():
        target, skip = _project_metadata_target(
            project_name,
            item,
            root=root,
            is_recent=project_name in recent_keys,
        )
        if target is not None:
            candidates.append(target)
        skipped.extend(skip)

    candidates = sorted(candidates, key=lambda item: item.path)
    action, selected = _resolve_interactive_selection(
        action, candidates, target_kind="project"
    )
    manifest_path, action_errors = _apply_action(
        action,
        [
            Path(cleanup_path)
            for target in selected
            for cleanup_path in (target.cleanup_paths or [target.path])
        ],
        target_kind="project",
    )
    return _payload(
        command="cleanup project",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(
                _dedupe_skips(skipped), key=lambda item: (item.kind, item.path)
            )
        ],
        manifest_path=manifest_path,
        filters={
            "older_than": _format_timedelta(args.older_than),
            "cutoff": cutoff.isoformat(),
            "path": str(root) if root else None,
        },
        discovery_note=None,
        action_errors=action_errors,
    )


def _handle_session(args: argparse.Namespace) -> dict[str, Any]:
    action = _resolve_action(args)
    vendor_filter = _normalize_vendor(args.agent_vendor)
    candidates: list[SessionTarget] = []
    skipped: list[SkippedTarget] = []
    for vendor, adapter_cls, base_dir in _session_sources(vendor_filter):
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
            target, skip = _session_target(path, vendor=vendor, adapter=adapter_cls())
            if target is not None:
                candidates.append(target)
            if skip is not None:
                skipped.append(skip)

    candidates = sorted(candidates, key=lambda item: item.path)
    action, selected = _resolve_interactive_selection(
        action, candidates, target_kind="session"
    )
    manifest_path, action_errors = _apply_action(
        action, [Path(target.path) for target in selected], target_kind="session"
    )
    return _payload(
        command="cleanup session",
        action=action,
        targets=[target.model_dump(mode="json") for target in selected],
        candidate_count=len(candidates),
        skipped=[
            item.model_dump(mode="json")
            for item in sorted(
                _dedupe_skips(skipped), key=lambda item: (item.kind, item.path)
            )
        ],
        manifest_path=manifest_path,
        filters={"agent_vendor": vendor_filter},
        discovery_note=None,
        action_errors=action_errors,
    )


def _project_metadata_target(
    project_name: str,
    item: dict[str, Any],
    *,
    root: Path | None,
    is_recent: bool,
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

    if is_recent:
        skips.append(_skip("project", str(resolved), "newer_than_retention"))
    else:
        reasons.append("older_than_retention")

    source_paths = _project_source_paths(item)
    if not resolved.exists():
        if source_paths:
            reasons.append("project_path_missing")
        else:
            skips.append(_skip("project", str(resolved), "project_path_missing"))
    else:
        if not _looks_like_project_directory(resolved):
            skips.append(_skip("project", str(resolved), "not_project_directory"))
        if (resolved / ".ct-cleanup-keep").exists():
            skips.append(_skip("project", str(resolved), "keep_marker"))
        git_skips = _git_skip_reasons(resolved)
        skips.extend(_skip("project", str(resolved), reason) for reason in git_skips)

    if skips:
        return None, skips

    cleanup_paths = (
        [str(path) for path in source_paths] if not resolved.exists() else []
    )
    return (
        ProjectTarget(
            project=project_name,
            path=str(resolved),
            cleanup_paths=cleanup_paths,
            bytes=sum(_path_size(path) for path in source_paths)
            if cleanup_paths
            else _path_size(resolved),
            reason=reasons or ["old_project"],
            last_activity_at=None,
            session_count=len(source_paths) if cleanup_paths else 0,
            vendors=sorted(item.get("vendors") or []),
        ),
        [],
    )


def _session_target(
    path: Path,
    *,
    vendor: Vendor,
    adapter: Any,
) -> tuple[SessionTarget | None, SkippedTarget | None]:
    if _recently_modified(path, timedelta(hours=24)):
        return None, _skip("session", str(path), "modified_in_last_24h")
    records = _load_jsonl(path)
    if records is None:
        return None, _skip("session", str(path), "unreadable_or_invalid")

    session: Session | None = None
    try:
        session = adapter.ingest_file(path)
    except Exception:
        if _has_useful_session_records(vendor, records):
            return None, _skip("session", str(path), "parse_failed")

    if session is not None:
        if session.parent_session_id is not None:
            return None, _skip("session", str(path), "has_parent_session")
        if session.status in {SessionStatus.ACTIVE, SessionStatus.INCOMPLETE}:
            return None, _skip("session", str(path), f"status_{session.status.value}")
        if session.turns or _has_useful_events(session):
            return None, None

    if _has_useful_session_records(vendor, records):
        return None, None

    return (
        SessionTarget(
            vendor=vendor.value,
            path=str(path.resolve()),
            bytes=_path_size(path),
            reason=["empty"],
            modified_at=_modified_at(path),
            session_id=str(session.session_id) if session is not None else None,
        ),
        None,
    )


def _project_source_paths(item: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for raw_path in item.get("sources") or []:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if path.exists():
            paths.append(path)
    return sorted(set(paths))


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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "summary": {
            "target_count": len(targets),
            "candidate_count": candidate_count,
            "target_bytes": sum(int(item.get("bytes") or 0) for item in targets),
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
    bytes_by_key: dict[tuple[str, str], int] = {}
    for item in skipped:
        key = (item.kind, item.path)
        merged.setdefault(key, set()).update(item.reason)
        bytes_by_key[key] = max(bytes_by_key.get(key, 0), item.bytes)
    return [
        SkippedTarget(
            kind=kind,
            path=path,
            bytes=bytes_by_key[(kind, path)],
            reason=sorted(reasons),
        )
        for (kind, path), reasons in merged.items()
    ]


def _resolve_action(args: argparse.Namespace) -> Action:
    if getattr(args, "delete", False):
        action: Action = "delete"
    elif getattr(args, "trash", False):
        action = "trash"
    else:
        return "interactive"
    if not getattr(args, "confirm", False):
        raise ValueError(f"--{action} requires --confirm")
    return action


def _resolve_interactive_selection(
    action: Action,
    candidates: list[ProjectTarget | SessionTarget],
    *,
    target_kind: str,
) -> tuple[Action, list[ProjectTarget | SessionTarget]]:
    if action != "interactive":
        return action, candidates
    if not candidates:
        return "cancelled", []

    _print_interactive_candidates(candidates, target_kind=target_kind)
    raw_selection = (
        input("Select candidates to clean [numbers, a=all, q=cancel]: ").strip().lower()
    )
    if raw_selection in {"", "q", "quit", "cancel"}:
        return "cancelled", []
    if raw_selection == "a":
        selected = candidates
    else:
        selected = _selected_candidates(candidates, raw_selection)
    if not selected:
        print("No candidates selected.", file=sys.stderr)
        return "cancelled", []

    raw_action = input("Action [t=trash, d=delete, q=cancel]: ").strip().lower()
    if raw_action in {"t", "trash"}:
        selected_action: Action = "trash"
    elif raw_action in {"d", "delete"}:
        selected_action = "delete"
    else:
        return "cancelled", []

    confirmation = (
        input(f"Type {selected_action} to confirm {len(selected)} item(s): ")
        .strip()
        .lower()
    )
    if confirmation != selected_action:
        return "cancelled", []
    return selected_action, selected


def _print_interactive_candidates(
    candidates: list[ProjectTarget | SessionTarget],
    *,
    target_kind: str,
) -> None:
    print(f"Cleanup {target_kind}: {len(candidates)} candidate(s)")
    for index, candidate in enumerate(candidates, start=1):
        label = (
            getattr(candidate, "project", None)
            or getattr(candidate, "vendor", None)
            or target_kind
        )
        print(f"{index:>3}. {label}  {_format_bytes(candidate.bytes)}")
        print(f"     {candidate.path}")


def _selected_candidates(
    candidates: list[ProjectTarget | SessionTarget],
    raw_selection: str,
) -> list[ProjectTarget | SessionTarget]:
    indexes: set[int] = set()
    for part in re.split(r"[\s,]+", raw_selection):
        if not part:
            continue
        try:
            index = int(part)
        except ValueError:
            continue
        if 1 <= index <= len(candidates):
            indexes.add(index)
    return [
        candidate
        for index, candidate in enumerate(candidates, start=1)
        if index in indexes
    ]


def _apply_action(
    action: Action,
    paths: list[Path],
    *,
    target_kind: str,
) -> tuple[str | None, list[dict[str, str]]]:
    if action in {"interactive", "cancelled"} or not paths:
        return None, []
    manifest = {
        "action": action,
        "target_kind": target_kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paths": [str(path) for path in paths],
        "errors": [],
    }
    errors: list[dict[str, str]] = []
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


def _write_manifest(manifest: dict[str, Any]) -> Path:
    directory = Path.home() / ".coding-trajectory" / "cleanup-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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


def _session_sources(vendor_filter: str | None) -> list[tuple[Vendor, type, Path]]:
    home = Path.home()
    sources: list[tuple[Vendor, type, Path]] = [
        (Vendor.CODEX_CLI, CodexAdapter, home / ".codex" / "sessions"),
        (Vendor.PI, PiAdapter, home / ".pi" / "agent" / "sessions"),
    ]
    if vendor_filter is None:
        return sources
    return [source for source in sources if source[0].value == vendor_filter]


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


def _has_useful_events(session: Session) -> bool:
    useful_types = {
        EventType.USER_PROMPT_SUBMITTED,
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_SUCCEEDED,
        EventType.TOOL_CALL_FAILED,
        EventType.LLM_RESPONSE,
    }
    return any(event.type in useful_types for event in session.events)


def _has_useful_session_records(vendor: Vendor, records: list[dict[str, Any]]) -> bool:
    if vendor == Vendor.CODEX_CLI:
        return any(_is_useful_codex_record(record) for record in records)
    if vendor == Vendor.PI:
        return any(_is_useful_pi_record(record) for record in records)
    return True


def _is_useful_codex_record(record: dict[str, Any]) -> bool:
    record_type = record.get("type")
    payload = record.get("payload")
    if record_type == "response_item":
        return True
    if record_type != "event_msg" or not isinstance(payload, dict):
        return False
    return payload.get("type") in {"user_message", "task_complete"}


def _is_useful_pi_record(record: dict[str, Any]) -> bool:
    if record.get("type") != "message":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("role") in {"user", "assistant", "toolResult", "bashExecution"}


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


def _recently_modified(path: Path, duration: timedelta) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return modified >= datetime.now(timezone.utc) - duration


def _modified_at(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


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


def _render_cleanup(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "detail", False):
        return json.dumps(payload, indent=2, ensure_ascii=False)
    summary = payload.get("summary") or {}
    lines = [
        payload.get("command", "cleanup"),
        f"Action: {payload.get('action') or 'interactive'}",
        f"Selected: {summary.get('target_count') or 0} of {summary.get('candidate_count') or 0}  Bytes: {_format_bytes(summary.get('target_bytes'))}",
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
            details.append(_format_bytes(item.get("bytes")))
            label = "  " + "  ".join(details)
            lines.append(label.rstrip())
            lines.append(f"    {item.get('path')}")
        if len(targets) > 20:
            lines.append(
                f"  ... {len(targets) - 20} more. Use --detail for the full list."
            )
    if payload.get("manifest_path"):
        lines.append(f"Manifest: {payload['manifest_path']}")
    errors = payload.get("errors") or []
    if errors:
        lines.append("Action errors:")
        for item in errors[:10]:
            lines.append(f"  {item.get('path')}: {item.get('error')}")
    return "\n".join(lines).rstrip()


def _format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}"


plugin = CleanupPlugin()
