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
    CloudflareCollectorRemote,
    CollectorIdentity,
    LocalCollector,
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
    load_profile,
    load_profile_credentials,
    profile_summary,
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


def _remote_from_args(args: argparse.Namespace) -> CloudflareCollectorRemote:
    _apply_credential_profile(args, require_token=True)
    url = args.cloudflare_url or os.environ.get("CT_CLOUDFLARE_URL")
    access_token = (
        args.access_token
        or os.environ.get("CT_COLLECTOR_ACCESS_TOKEN")
        or os.environ.get("CT_ACCESS_TOKEN")
    )
    missing = [
        name
        for name, value in (
            ("CLOUDFLARE_URL", url),
            ("COLLECTOR_ACCESS_TOKEN", access_token),
        )
        if not value
    ]
    if missing:
        raise ValueError("collector run requires " + ", ".join(missing))
    return CloudflareCollectorRemote(url=url, access_token=access_token)


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


def _apply_credential_profile(
    args: argparse.Namespace, *, require_token: bool = True
) -> None:
    profile_name = getattr(args, "credential_profile", None)
    profile_name = profile_name or os.environ.get("CT_CREDENTIAL_PROFILE")
    if not profile_name:
        return
    profile = load_profile(profile_name)
    if profile.role != "collector" or profile.agent_id is None:
        raise CollectorCredentialError(
            "collector operations require a collector profile with agent identity"
        )
    from coding_trajectory.control_plane.remote import cloudflare_endpoint

    for key in ("workspace_id", "agent_id"):
        value = getattr(args, key, None)
        if value is not None and str(value) != str(getattr(profile, key)):
            raise CollectorCredentialError(
                f"selected profile conflicts with --{key.replace('_', '-')}"
            )
    if args.cloudflare_url and cloudflare_endpoint(
        args.cloudflare_url
    ) != cloudflare_endpoint(str(profile.cloudflare_url)):
        raise CollectorCredentialError(
            "selected profile conflicts with --cloudflare-url"
        )
    if not hasattr(args, "_explicit_access_token"):
        args._explicit_access_token = args.access_token
    if args._explicit_access_token:
        raise CollectorCredentialError("selected profile conflicts with --access-token")
    args.workspace_id = args.workspace_id or profile.workspace_id
    args.agent_id = args.agent_id or profile.agent_id
    args.project_id = args.project_id or profile.project_id
    args.cloudflare_url = args.cloudflare_url or str(profile.cloudflare_url)
    args.project_name = args.project_name or profile.project_name
    args.state_path = args.state_path or profile.state_path
    if require_token:
        args.access_token = load_profile_credentials(profile_name).access_token


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
    args.workspace_id = args.workspace_id or (
        _uuid_arg(os.environ["CT_REMOTE_WORKSPACE_ID"])
        if os.environ.get("CT_REMOTE_WORKSPACE_ID")
        else None
    )
    args.agent_id = args.agent_id or (
        _uuid_arg(os.environ["CT_COLLECTOR_AGENT_ID"])
        if os.environ.get("CT_COLLECTOR_AGENT_ID")
        else None
    )
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
        raise ValueError("collector run requires --project-name for fact identity")
    identity = _identity_from_args(args, state_path, project_id=project_id)
    with LocalCollector(database_path=state_path, identity=identity) as collector:
        result = collector.collect(
            current_dir=Path.cwd(),
            global_scope=args.global_scope,
            agent_vendor=args.agent_vendor,
            since_days=args.since_days,
            remote=remote,
            heartbeat=not args.no_heartbeat,
            target_session_id=getattr(args, "session_id", None),
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
        "facts_queued": result.facts_queued,
        "facts_accepted": result.facts_accepted,
        "facts_rejected": result.facts_rejected,
        "fact_scope_incomplete": result.fact_scope_incomplete,
        "scope_remedy": (
            "Include all sources of overlapping published graphs within the authorized collection scope."
            if result.fact_scope_incomplete
            else None
        ),
    }


def _handle_status(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state_path)
    identity = CollectorIdentity(
        workspace_id=UUID(int=0), agent_id=UUID(int=0), agent_instance_id=UUID(int=0)
    )
    with LocalCollector(database_path=state_path, identity=identity) as collector:
        return {"pending": collector.pending_count()}


def _handle_publish(args: argparse.Namespace) -> dict[str, Any]:
    from coding_trajectory.control_plane.publication_run import (
        PublicationRun,
        PublicationStopped,
    )

    try:
        if args.publish_action == "status":
            return PublicationRun.status(Path(args.run_dir))
        with PublicationRun(
            Path(args.run_dir), create=args.publish_action == "plan"
        ) as run:
            if args.publish_action == "plan":
                return run.plan(
                    source_sha=args.source_sha,
                    worker_version=args.worker_version,
                    credential_profile=args.credential_profile,
                    workspace_id=args.workspace_id,
                    project_id=args.project_id,
                    project_name=args.project_name,
                    project_root=Path(args.project_root),
                )
            if args.publish_action == "reconcile":
                return run.reconcile()
            return run.execute(getattr(args, "reconciliation_sha", None))
    except PublicationStopped as exc:
        raise ValueError(str(exc)) from None


def _handle_credentials_configure(args: argparse.Namespace) -> dict[str, Any]:
    token = (
        None
        if args.token_env
        else (
            sys.stdin.readline().rstrip("\n")
            if args.token_stdin
            else getpass.getpass("Cloudflare collector token: ")
        )
    )
    if not args.token_env and not token:
        raise CollectorCredentialError("collector token must not be empty")
    configure_profile(
        profile_name=args.profile,
        cloudflare_url=args.cloudflare_url,
        token=token,
        token_env=args.token_env,
        workspace_id=args.workspace_id,
        agent_id=args.agent_id,
        project_id=args.project_id,
    )
    return {
        "profile": args.profile,
        "configured": True,
        "token_storage": "environment" if args.token_env else "macOS Keychain",
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
        help="Queue checkpoints and publish bounded typed fact rows.",
        formatter_class=GhFormatter,
    )
    add_agent_vendor_flag(run)
    run.add_argument(
        "--session-id",
        type=_uuid_arg,
        help="Publish only the requested session's complete canonical graph.",
    )
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
    run.add_argument("--cloudflare-url", help="Defaults to CT_CLOUDFLARE_URL.")
    run.add_argument(
        "--access-token",
        help="Defaults to CT_COLLECTOR_ACCESS_TOKEN, then CT_ACCESS_TOKEN.",
    )
    run.add_argument(
        "--credential-profile",
        default=argparse.SUPPRESS,
        help="Load a collector profile (defaults to CT_CREDENTIAL_PROFILE) before publishing.",
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

    publish = commands.add_parser(
        "publish",
        help="Plan, execute and reconcile a pinned complete-project publication.",
        formatter_class=GhFormatter,
    )
    publication_actions = publish.add_subparsers(dest="publish_action", required=True)
    for action in ("plan", "start", "status", "reconcile", "resume"):
        operation = publication_actions.add_parser(action, formatter_class=GhFormatter)
        operation.add_argument(
            "--run-dir",
            required=True,
            help="Private directory unique to this publication.",
        )
        operation.set_defaults(_plugin_handler=_handle_publish, _default_output="json")
        if action == "plan":
            operation.add_argument(
                "--source-sha",
                required=True,
                help="Full clean collector checkout commit SHA.",
            )
            operation.add_argument(
                "--worker-version",
                required=True,
                type=_uuid_arg,
                help="Expected deployed Cloudflare Worker version ID.",
            )
            operation.add_argument("--credential-profile", required=True)
            operation.add_argument("--workspace-id", required=True, type=_uuid_arg)
            operation.add_argument("--project-id", required=True, type=_uuid_arg)
            operation.add_argument("--project-name", required=True)
            operation.add_argument(
                "--project-root",
                required=True,
                help="Local discovery root; no age or partial-session filter.",
            )
        elif action == "resume":
            operation.add_argument(
                "--reconciliation-sha",
                required=True,
                help="Fresh digest returned by read-only reconcile.",
            )

    credentials = commands.add_parser(
        "credentials",
        help="Configure private, scoped collector credentials.",
        formatter_class=GhFormatter,
    )
    credential_commands = credentials.add_subparsers(
        dest="collector_credentials_action", required=True
    )
    configure = credential_commands.add_parser(
        "configure",
        help="Configure Keychain storage or an injected token environment variable.",
        formatter_class=GhFormatter,
    )
    configure.add_argument("--profile", default="default")
    configure.add_argument("--workspace-id", required=True, type=_uuid_arg)
    configure.add_argument("--agent-id", required=True, type=_uuid_arg)
    configure.add_argument("--project-id", type=_uuid_arg)
    configure.add_argument("--cloudflare-url", required=True)
    secret_source = configure.add_mutually_exclusive_group()
    secret_source.add_argument(
        "--token-env",
        help="Read the token from this environment variable at run time; store only its name.",
    )
    secret_source.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the token from standard input.",
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
