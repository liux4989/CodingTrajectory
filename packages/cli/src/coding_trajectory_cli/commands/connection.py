"""Shared connection onboarding and read-only authentication checks."""

from __future__ import annotations

import argparse
import getpass
from typing import Any
from uuid import UUID

from coding_trajectory.control_plane.connections import (
    ConnectionError,
    configure_profile,
    forget_profile,
    load_profile_credentials,
    profile_summary,
    rotate_profile,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)
from pydantic import BaseModel, ConfigDict


class ConnectionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    agent_id: UUID
    roles: list[str]
    protocol: str


def _handle(args: argparse.Namespace) -> dict[str, Any]:
    name = args.name
    if args.action == "configure":
        token = None
        if args.role != "local" and not args.token_env:
            token = getpass.getpass("Access token: ")
        try:
            configure_profile(
                profile_name=name,
                cloudflare_url=args.url,
                workspace_id=args.workspace_id,
                agent_id=args.agent_id,
                project_id=args.project_id,
                project_name=args.project_name,
                state_path=args.state_path,
                token=token,
                token_env=args.token_env,
                role=args.role,
                default_source=args.default_source
                or ("local" if args.role == "local" else "shared"),
            )
        except ValueError:
            raise ConnectionError(
                "invalid connection profile; check endpoint and required identities"
            ) from None
        return profile_summary(name)
    if args.action == "rotate":
        rotate_profile(
            name,
            token_env=args.token_env,
            token=None if args.token_env else getpass.getpass("New access token: "),
        )
        return {**profile_summary(name), "server_revoked": False}
    if args.action == "forget":
        forget_profile(name)
        return {"profile": name, "configured": False, "server_revoked": False}
    if args.action == "status":
        return profile_summary(name)
    credentials = load_profile_credentials(name)
    client = CloudflareRpcClient(
        url=str(credentials.profile.cloudflare_url),
        access_token=credentials.access_token,
    )
    try:
        if args.action == "migrate":
            if (
                credentials.profile.role != "collector"
                or not credentials.profile.agent_id
            ):
                raise ConnectionError(
                    "catalog migration requires a collector connection"
                )
            migrated = 0
            for _ in range(args.max_pages):
                page = client.call(
                    "ct_catalog_migrate",
                    {
                        "workspace_id": str(credentials.profile.workspace_id),
                        "agent_id": str(credentials.profile.agent_id),
                    },
                )
                migrated += int(page["migrated"])
                if page["complete"]:
                    return {"profile": name, "complete": True, "migrated": migrated}
            return {
                "profile": name,
                "complete": False,
                "migrated": migrated,
                "next_action": "repeat connection migrate",
            }
        result = ConnectionStatus.model_validate(
            client.call(
                "ct_connection_status",
                {"workspace_id": str(credentials.profile.workspace_id)},
            )
        )
    except RemoteControlPlaneError as exc:
        code = {401: "authentication rejected", 403: "capability denied"}.get(
            exc.status, "endpoint unavailable or incompatible"
        )
        raise ConnectionError(f"connection check failed: {code}") from None
    except ValueError:
        # Never include remote response bodies, token values, or request headers.
        raise ConnectionError(
            "connection check failed; verify endpoint, credential, and server capability support"
        ) from None
    finally:
        client.close()
    if (
        result.workspace_id != credentials.profile.workspace_id
        or result.protocol != "ct.core.v1"
    ):
        raise ConnectionError("connection identity or protocol mismatch")
    if credentials.profile.agent_id and result.agent_id != credentials.profile.agent_id:
        raise ConnectionError("connection collector identity mismatch")
    required = "collect" if credentials.profile.role == "collector" else "read"
    if required not in result.roles and "owner" not in result.roles:
        raise ConnectionError("connection capability denied for configured role")
    return {
        "profile": name,
        "authenticated": True,
        "roles": result.roles,
        "protocol": result.protocol,
        "identity_matches": True,
    }


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "connection", help="Configure shared collection and query connections."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("configure", "status", "check", "rotate", "forget", "migrate"):
        command = commands.add_parser(action)
        command.add_argument("name", nargs="?", default="default")
        command.set_defaults(_plugin_handler=_handle, _default_output="json")
        if action == "migrate":
            command.add_argument(
                "--max-pages", type=int, choices=range(1, 101), default=20
            )
        if action in {"configure", "rotate"}:
            command.add_argument(
                "--token-env",
                help="Environment variable containing the token; otherwise prompt securely.",
            )
        if action == "configure":
            command.add_argument("--url")
            command.add_argument("--workspace-id", type=UUID)
            command.add_argument("--agent-id", type=UUID)
            command.add_argument("--project-id", type=UUID)
            command.add_argument("--project-name")
            command.add_argument("--state-path")
            command.add_argument(
                "--role", choices=("collector", "reader", "local"), default="reader"
            )
            command.add_argument(
                "--default-source", choices=("local", "shared", "auto")
            )
