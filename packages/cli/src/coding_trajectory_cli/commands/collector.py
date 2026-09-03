"""Commands for the host-local remote-control-plane collector."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.control_plane.collector import (
    CollectorIdentity,
    LocalCollector,
    SupabaseCollectorRemote,
)
from coding_trajectory.control_plane.collector_protocol import (
    ProjectRegistrationRequest,
)
from coding_trajectory.discovery import discover_source_candidates

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_agent_vendor_flag,
    add_global_scope_flag,
)
from coding_trajectory_cli.collector_credentials import (
    CollectorCredentialError,
    configure_profile,
    profile_summary,
    refresh_profile,
)


def _uuid_arg(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _state_path(value: str | None) -> Path:
    return (
        Path(value).expanduser()
        if value
        else Path("~/.coding-trajectory/control-plane/collector.sqlite3").expanduser()
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _remote_from_args(args: argparse.Namespace) -> SupabaseCollectorRemote:
    url = args.supabase_url or os.environ.get("CT_SUPABASE_URL")
    api_key = args.supabase_api_key or os.environ.get("CT_SUPABASE_ANON_KEY")
    access_token = args.access_token or os.environ.get("CT_COLLECTOR_ACCESS_TOKEN")
    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", url),
            ("SUPABASE_ANON_KEY", api_key),
            ("COLLECTOR_ACCESS_TOKEN", access_token),
        )
        if not value
    ]
    if missing:
        raise ValueError("collector run requires " + ", ".join(missing))
    return SupabaseCollectorRemote(url=url, api_key=api_key, access_token=access_token)


def _identity_from_args(
    args: argparse.Namespace, state_path: Path, *, project_id: UUID | None = None
) -> CollectorIdentity:
    agent_instance_id = args.agent_instance_id or uuid5(
        NAMESPACE_URL, f"ct-collector:{args.agent_id}:{state_path.resolve()}"
    )
    return CollectorIdentity(
        workspace_id=args.workspace_id,
        agent_id=args.agent_id,
        agent_instance_id=agent_instance_id,
        project_id=project_id if project_id is not None else args.project_id,
        project_name=args.project_name,
    )


def _apply_credential_profile(args: argparse.Namespace) -> None:
    profile_name = getattr(args, "credential_profile", None)
    if not profile_name:
        return
    refreshed = refresh_profile(profile_name)
    profile = refreshed.profile
    args.workspace_id = args.workspace_id or profile.workspace_id
    args.agent_id = args.agent_id or profile.agent_id
    args.project_id = args.project_id or profile.project_id
    args.supabase_url = args.supabase_url or str(profile.supabase_url)
    args.supabase_api_key = args.supabase_api_key or profile.supabase_api_key
    args.access_token = args.access_token or refreshed.access_token


def _require_identity(args: argparse.Namespace) -> None:
    missing = [
        name
        for name, value in (
            ("workspace-id", args.workspace_id),
            ("agent-id", args.agent_id),
        )
        if value is None
    ]
    if missing:
        raise ValueError("collector run requires " + ", ".join(missing))


def _handle_scan(args: argparse.Namespace) -> dict[str, Any]:
    candidates = discover_source_candidates(
        current_dir=Path.cwd(),
        global_scope=args.global_scope,
        agent_vendor=args.agent_vendor,
        since_days=args.since_days,
    )
    return {
        "discovered": len(candidates),
        "by_vendor": dict(
            sorted(Counter(item.vendor.value for item in candidates).items())
        ),
        "note": "No source paths or vendor content are emitted.",
    }


def _handle_run(args: argparse.Namespace) -> dict[str, Any]:
    _apply_credential_profile(args)
    _require_identity(args)
    state_path = _state_path(args.state_path)
    remote = _remote_from_args(args)
    project_id = args.project_id
    if args.project_name:
        registration = remote.register_project(
            ProjectRegistrationRequest(
                workspace_id=args.workspace_id,
                agent_id=args.agent_id,
                display_name=args.project_name,
                repository_identity=args.repository_identity,
                aliases=args.project_alias,
            )
        )
        if project_id is not None and registration.project_id != project_id:
            raise ValueError(
                "registered project does not match the configured --project-id"
            )
        project_id = registration.project_id
    if project_id is None:
        raise ValueError("collector run requires --project-id or --project-name")
    if not args.project_name:
        raise ValueError("collector run requires --project-name for artifact identity")
    identity = _identity_from_args(args, state_path, project_id=project_id)
    with LocalCollector(database_path=state_path, identity=identity) as collector:
        result = collector.collect(
            current_dir=Path.cwd(),
            global_scope=args.global_scope,
            agent_vendor=args.agent_vendor,
            since_days=args.since_days,
            remote=remote,
            heartbeat=not args.no_heartbeat,
        )
    return {
        "project_id": str(project_id),
        "discovered": result.discovered,
        "queued": result.queued,
        "accepted": result.accepted,
        "rejected": result.rejected,
        "pending": result.pending,
        "heartbeat_sequence": result.heartbeat_sequence,
        "failed": result.failed,
        "artifacts_queued": result.artifacts_queued,
        "artifacts_accepted": result.artifacts_accepted,
        "artifacts_rejected": result.artifacts_rejected,
    }


def _handle_status(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state_path)
    identity = CollectorIdentity(
        workspace_id=UUID(int=0), agent_id=UUID(int=0), agent_instance_id=UUID(int=0)
    )
    with LocalCollector(database_path=state_path, identity=identity) as collector:
        return {"pending": collector.pending_count()}


def _handle_credentials_configure(args: argparse.Namespace) -> dict[str, Any]:
    password = (
        sys.stdin.readline().rstrip("\n")
        if args.password_stdin
        else getpass.getpass("Collector Auth password: ")
    )
    if not password:
        raise CollectorCredentialError("collector password must not be empty")
    configure_profile(
        profile_name=args.profile,
        supabase_url=args.supabase_url,
        supabase_api_key=args.supabase_api_key,
        email=args.email,
        password=password,
        workspace_id=args.workspace_id,
        agent_id=args.agent_id,
        project_id=args.project_id,
    )
    return {
        "profile": args.profile,
        "configured": True,
        "password_storage": "macOS Keychain",
    }


def _handle_credentials_status(args: argparse.Namespace) -> dict[str, Any]:
    return profile_summary(args.profile)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "collector",
        prog="ct collector",
        help="Collect local vendor logs through the remote control-plane ingress.",
        formatter_class=GhFormatter,
    )
    commands = parser.add_subparsers(dest="collector_action", required=True)

    scan = commands.add_parser(
        "scan",
        help="Count eligible local sources without reading content.",
        formatter_class=GhFormatter,
    )
    add_global_scope_flag(scan)
    add_agent_vendor_flag(scan)
    scan.add_argument(
        "--since-days",
        type=_positive_int,
        help="Include sources modified in the last N days.",
    )
    scan.set_defaults(_plugin_handler=_handle_scan, _default_output="json")

    run = commands.add_parser(
        "run",
        help="Queue checkpoints and publish bounded shareable graph artifacts.",
        formatter_class=GhFormatter,
    )
    add_agent_vendor_flag(run)
    run.add_argument(
        "--since-days",
        type=_positive_int,
        default=7,
        help="Publish only sources modified in the last N days (default: 7).",
    )
    run.add_argument("--workspace-id", type=_uuid_arg)
    run.add_argument("--agent-id", type=_uuid_arg)
    run.add_argument("--project-id", type=_uuid_arg)
    run.add_argument(
        "--project-name",
        help="Register this portable project display name before publishing.",
    )
    run.add_argument(
        "--repository-identity",
        help="Optional portable repository identity; never a host path.",
    )
    run.add_argument(
        "--project-alias",
        action="append",
        default=[],
        help="Portable project alias; repeat for multiple aliases.",
    )
    run.add_argument("--agent-instance-id", type=_uuid_arg)
    run.add_argument("--state-path", help="Private SQLite delivery state path.")
    run.add_argument("--supabase-url", help="Defaults to CT_SUPABASE_URL.")
    run.add_argument("--supabase-api-key", help="Defaults to CT_SUPABASE_ANON_KEY.")
    run.add_argument("--access-token", help="Defaults to CT_COLLECTOR_ACCESS_TOKEN.")
    run.add_argument(
        "--credential-profile",
        help="Refresh a private macOS Keychain-backed collector profile before publishing.",
    )
    run.add_argument("--no-heartbeat", action="store_true")
    run.set_defaults(
        _plugin_handler=_handle_run,
        _default_output="json",
        global_scope=False,
    )

    status = commands.add_parser(
        "status",
        help="Show local outbox count only.",
        formatter_class=GhFormatter,
    )
    status.add_argument("--state-path", help="Private SQLite delivery state path.")
    status.set_defaults(_plugin_handler=_handle_status, _default_output="json")

    credentials = commands.add_parser(
        "credentials",
        help="Configure private, refreshable collector credentials.",
        formatter_class=GhFormatter,
    )
    credential_commands = credentials.add_subparsers(
        dest="collector_credentials_action", required=True
    )
    configure = credential_commands.add_parser(
        "configure",
        help="Store a collector password in macOS Keychain.",
        formatter_class=GhFormatter,
    )
    configure.add_argument("--profile", default="default")
    configure.add_argument("--workspace-id", required=True, type=_uuid_arg)
    configure.add_argument("--agent-id", required=True, type=_uuid_arg)
    configure.add_argument("--project-id", type=_uuid_arg)
    configure.add_argument("--supabase-url", required=True)
    configure.add_argument("--supabase-api-key", required=True)
    configure.add_argument("--email", required=True)
    configure.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from standard input.",
    )
    configure.set_defaults(
        _plugin_handler=_handle_credentials_configure, _default_output="json"
    )

    credential_status = credential_commands.add_parser(
        "status",
        help="Show whether a private collector profile is configured.",
        formatter_class=GhFormatter,
    )
    credential_status.add_argument("--profile", default="default")
    credential_status.set_defaults(
        _plugin_handler=_handle_credentials_status, _default_output="json"
    )
