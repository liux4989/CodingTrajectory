"""Managed host collection; supervisor manifests contain no credentials."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import plistlib
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
    CollectorRemoteError,
)
from coding_trajectory.control_plane.connections import (
    CollectorCredentialError,
    load_profile,
    load_profile_credentials,
    profile_path,
)
from coding_trajectory.control_plane.upload_service import UploadService
from coding_trajectory.control_plane.upload_state import (
    UploadStateError,
    default_upload_state_path,
)
from pydantic import BaseModel, ConfigDict, Field

LABEL = "com.codingtrajectory.collector"
NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
RECOVERABLE = (RuntimeError, ValueError, OSError, sqlite3.Error)


class ServiceProject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=NAME)
    profile: str = Field(pattern=NAME)
    directory: Path
    state_path: Path
    workspace_id: UUID
    agent_id: UUID
    project_name: str = Field(min_length=1, max_length=256)
    project_id: UUID | None = None
    agent_vendor: str | None = None

    def identity(self) -> CollectorIdentity:
        return CollectorIdentity(
            workspace_id=self.workspace_id,
            agent_id=self.agent_id,
            agent_instance_id=uuid5(
                NAMESPACE_URL, f"ct-collector:{self.agent_id}:{self.state_path}"
            ),
            project_id=self.project_id,
            project_name=self.project_name,
        )


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    connection_dir: Path
    projects: list[ServiceProject] = Field(default_factory=list, max_length=64)
    poll_seconds: int = Field(default=10, ge=1, le=3600)
    secret_file: Path | None = None


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("collector_service_busy") from None
        yield
    finally:
        os.close(fd)


def _fault(error: BaseException) -> str:
    if isinstance(error, (UploadStateError, CollectorRemoteError)):
        return error.code
    if isinstance(error, CollectorCredentialError):
        return "credential_unavailable"
    if isinstance(error, FileNotFoundError):
        return "project_directory_unavailable"
    return "collector_operation_failed"


def remedy(code: str | None) -> str | None:
    if not code:
        return None
    if code in {
        "credential_unavailable",
        "authentication_required",
        "capability_required",
        "agent_denied",
    }:
        return "Check the connection role and rotate or inject its credential; pending batches are retained."
    if code in {"remote_state_reset", "authority_incarnation_changed"}:
        return "Inspect the authority restore, then explicitly reconcile remote state."
    if code in {"upload_backpressure", "canonical_backpressure"}:
        return "Drain pending publication or increase the storage budget; do not delete the outbox."
    if code.endswith("owner_busy") or code == "collector_service_busy":
        return "Stop the duplicate collector using this state before retrying."
    return "Inspect collection status and the source/profile configuration; do not reset delivery state."


class HostService:
    def __init__(self, root: Path | None = None):
        self.root = (
            (
                root
                or Path(
                    os.environ.get(
                        "CT_COLLECTOR_SERVICE_DIR",
                        "~/.coding-trajectory/control-plane/service",
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        self.config_path = self.root / "service.json"

    def config(self) -> ServiceConfig:
        if not self.config_path.exists():
            return ServiceConfig(
                connection_dir=profile_path("default").parent.resolve()
            )
        try:
            return ServiceConfig.model_validate_json(self.config_path.read_bytes())
        except (ValueError, OSError):
            raise RuntimeError("collector_service_configuration_invalid") from None

    def save(self, config: ServiceConfig) -> None:
        _atomic(self.config_path, config.model_dump_json(indent=2).encode())

    def add(
        self,
        *,
        name: str,
        profile: str,
        directory: Path,
        project_name: str | None = None,
        state_path: Path | None = None,
        agent_vendor: str | None = None,
    ) -> dict[str, Any]:
        with _lock(self.root / "configuration.lock"):
            config = self.config()
            connection = load_profile(profile)
            if (
                connection.role != "collector"
                or not connection.agent_id
                or not connection.workspace_id
            ):
                raise CollectorCredentialError("a collector connection is required")
            if profile_path(profile).parent.resolve() != config.connection_dir:
                raise ValueError(
                    "service connection directory differs from selected profile"
                )
            directory = directory.expanduser().resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("project directory is required")
            title = project_name or connection.project_name
            if not title:
                raise ValueError("project name is required in the profile or command")
            delivery = (
                (
                    state_path
                    or (Path(connection.state_path) if connection.state_path else None)
                    or default_upload_state_path(
                        workspace_id=connection.workspace_id,
                        agent_id=connection.agent_id,
                        project_name=title,
                    )
                )
                .expanduser()
                .resolve()
            )
            project = ServiceProject(
                name=name,
                profile=profile,
                directory=directory,
                state_path=delivery,
                workspace_id=connection.workspace_id,
                agent_id=connection.agent_id,
                project_id=connection.project_id,
                project_name=title,
                agent_vendor=agent_vendor,
            )
            if any(p.name == name or p.state_path == delivery for p in config.projects):
                raise ValueError("project name or delivery state is already registered")
            # Validate an existing database's identity; never recreate it.
            state = UploadService(delivery, project.identity())
            state.close()
            self.save(
                ServiceConfig.model_validate(
                    {**config.model_dump(), "projects": [*config.projects, project]}
                )
            )
        return {
            "registered": name,
            "publication_mode": "preserved",
            "schedule_activated": False,
        }

    def remove(self, name: str) -> dict[str, Any]:
        # A stopped service cannot publish a project after it is removed.
        with _lock(self.root / "run.lock"), _lock(self.root / "configuration.lock"):
            config = self.config()
            if not any(p.name == name for p in config.projects):
                raise ValueError("project is not registered")
            config.projects = [p for p in config.projects if p.name != name]
            self.save(config)
        return {"removed": name, "delivery_state_preserved": True}

    def _profile(self, project: ServiceProject):
        config = self.config()
        if profile_path(project.profile).parent.resolve() != config.connection_dir:
            raise CollectorCredentialError("service connection directory changed")
        profile = load_profile(project.profile)
        if (
            profile.role != "collector"
            or profile.workspace_id != project.workspace_id
            or profile.agent_id != project.agent_id
        ):
            raise UploadStateError("upload_identity_conflict")
        return profile

    def remote(self, project: ServiceProject) -> CloudflareCollectorRemote:
        profile = self._profile(project)
        secret_file = self.config().secret_file
        if profile.token_env and secret_file:
            try:
                info = secret_file.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 65536
                ):
                    raise ValueError
                secrets = json.loads(secret_file.read_bytes())
                token = secrets[profile.token_env]
                if not isinstance(token, str) or not token:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise CollectorCredentialError(
                    "private secret file is unavailable or invalid"
                ) from None
        else:
            token = load_profile_credentials(project.profile).access_token
        return CloudflareCollectorRemote(
            url=str(profile.cloudflare_url), access_token=token, chunked=True
        )

    def policy(
        self,
        *,
        mode: str | None = None,
        paused: bool | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        projects = [
            p
            for p in self.config().projects
            if project_name is None or p.name == project_name
        ]
        if not projects:
            raise ValueError("no matching registered project")
        for project in projects:
            self._profile(project)
        for project in projects:
            service = UploadService(project.state_path, project.identity())
            try:
                changes: dict[str, Any] = {}
                if mode is not None:
                    changes["mode"] = mode
                if paused is not None:
                    changes["paused"] = paused
                service.configure(**changes)
            finally:
                service.close()
        return self.status()

    def running(self) -> bool:
        try:
            with _lock(self.root / "run.lock"):
                return False
        except RuntimeError:
            return True

    def status(self) -> dict[str, Any]:
        try:
            runtime = json.loads((self.root / "runtime.json").read_bytes())
        except (OSError, ValueError):
            runtime = {}
        running = self.running()
        heartbeat_age = None
        if running and runtime.get("heartbeat_at"):
            try:
                heartbeat_age = max(
                    0.0,
                    (
                        datetime.now(UTC)
                        - datetime.fromisoformat(runtime["heartbeat_at"])
                    ).total_seconds(),
                )
            except (TypeError, ValueError):
                pass
        projects = []
        for project in self.config().projects:
            status: dict[str, Any] = {}
            code = None
            try:
                self._profile(project)
                if project.state_path.exists():
                    service = UploadService(project.state_path, project.identity())
                    try:
                        status = service.status()
                    finally:
                        service.close()
            except RECOVERABLE as error:
                code = _fault(error)
            progress = (
                runtime.get("projects", {}).get(project.name, {}) if running else {}
            )
            code = (
                code or status.get("preparation_error") or status.get("authority_error")
            )
            code = code or next(
                (item["error_code"] for item in status.get("blocked", [])), None
            )
            code = (
                code or progress.get("prepare_error") or progress.get("delivery_error")
            )
            projects.append(
                {
                    "name": project.name,
                    "profile": project.profile,
                    **status,
                    "progress": progress,
                    "error_code": code,
                    "action": remedy(code),
                }
            )
        return {
            "configured": self.config_path.exists(),
            "running": running,
            "heartbeat_at": runtime.get("heartbeat_at") if running else None,
            "heartbeat_age_seconds": heartbeat_age,
            "service_health": "stopped"
            if not running
            else "responsive"
            if heartbeat_age is not None and heartbeat_age <= 30
            else "unknown",
            "projects": projects,
            "network_requests": 0,
        }

    def run(self) -> dict[str, Any]:
        config = self.config()
        if not config.projects:
            raise ValueError("register a project before starting collection")
        os.environ["CT_CONNECTION_DIR"] = str(config.connection_dir)
        with _lock(self.root / "run.lock"):
            stop = threading.Event()
            guard = threading.Lock()
            progress: dict[str, dict[str, Any]] = {}

            def lane(operation: str):
                while not stop.is_set():
                    try:
                        current = self.config()
                        for project in current.projects:
                            if stop.is_set():
                                break
                            error_code = None
                            performed = False
                            service = None
                            try:
                                self._profile(project)
                                service = UploadService(
                                    project.state_path, project.identity()
                                )
                                if operation == "prepare":
                                    if not project.directory.is_dir():
                                        raise FileNotFoundError
                                    service.prepare(
                                        current_dir=project.directory,
                                        agent_vendor=project.agent_vendor,
                                    )
                                    performed = True
                                elif service.should_flush():
                                    remote = self.remote(project)
                                    try:
                                        result = service.publish(
                                            remote, max_batches=1, force=False
                                        )
                                        error_code = result.get("authority_error")
                                        performed = bool(result.get("published"))
                                    finally:
                                        remote.close()
                            except RECOVERABLE as error:
                                error_code = _fault(error)
                            finally:
                                if service:
                                    service.close()
                            with guard:
                                row = progress.setdefault(project.name, {})
                                row[f"{operation}_error"] = error_code
                                if performed:
                                    row[f"last_{operation}_at"] = _stamp()
                        stop.wait(current.poll_seconds)
                    except RECOVERABLE:
                        stop.wait(10)

            previous = {
                sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGTERM, signal.SIGINT)
            }
            workers = [
                threading.Thread(
                    target=lane,
                    args=(operation,),
                    daemon=True,
                    name=f"ct-host-{operation}",
                )
                for operation in ("prepare", "delivery")
            ]
            try:
                for worker in workers:
                    worker.start()
                while not stop.is_set():
                    with guard:
                        payload = {"heartbeat_at": _stamp(), "projects": dict(progress)}
                        _atomic(
                            self.root / "runtime.json", json.dumps(payload).encode()
                        )
                    if not all(worker.is_alive() for worker in workers):
                        raise RuntimeError("collector_service_worker_stopped")
                    stop.wait(1)
            finally:
                stop.set()
                deadline = time.monotonic() + 30
                for worker in workers:
                    worker.join(timeout=max(0, deadline - time.monotonic()))
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            return {"stopped": True, "pending_state_preserved": True}

    def install(self, *, secret_file: Path | None = None) -> dict[str, Any]:
        with _lock(self.root / "configuration.lock"):
            config = self.config()
            if secret_file is not None:
                config.secret_file = secret_file.expanduser().resolve()
            self.save(config)
            kind, payload = self.supervisor_template(platform.system())
            _atomic(self.root / kind, payload)
        return {
            "installed": True,
            "running": self.running(),
            "schedule_activated": False,
        }

    def supervisor_template(self, system: str) -> tuple[str, bytes]:
        command = [
            sys.executable,
            "-m",
            "coding_trajectory_cli.cli",
            "collector",
            "service",
            "run",
        ]
        env = {
            "CT_COLLECTOR_SERVICE_DIR": str(self.root),
            "CT_CONNECTION_DIR": str(self.config().connection_dir),
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        if system == "Darwin":
            return "supervisor.plist", plistlib.dumps(
                {
                    "Label": LABEL,
                    "ProgramArguments": command,
                    "EnvironmentVariables": env,
                    "WorkingDirectory": str(self.root),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "ThrottleInterval": 10,
                    "ExitTimeOut": 40,
                    "ProcessType": "Background",
                }
            )
        if system == "Linux":

            def quote(value: str) -> str:
                if "\n" in value or "\r" in value:
                    raise ValueError("invalid supervisor path")
                return (
                    '"'
                    + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
                    + '"'
                )

            return "supervisor.service", (
                "[Unit]\nDescription=CodingTrajectory collector\n"
                "[Service]\nType=simple\nExecStart="
                + " ".join(quote(v).replace("$", "$$") for v in command)
                + "\n"
                + "\n".join(f"Environment={quote(k + '=' + v)}" for k, v in env.items())
                + "\nRestart=always\nRestartSec=10\nTimeoutStopSec=40\nKillMode=control-group\n"
                "UMask=0077\n[Install]\nWantedBy=default.target\n"
            ).encode()
        raise RuntimeError(
            "managed collection supports macOS launchd and Linux systemd"
        )

    def supervisor(self, action: Literal["enable", "disable"]) -> dict[str, Any]:
        system = platform.system()
        kind, _ = self.supervisor_template(system)
        template = self.root / kind
        if action == "enable" and not template.exists():
            self.install()
        if action == "enable" and not self.config().projects:
            raise ValueError("register a project before enabling collection")
        if system == "Darwin":
            target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
            domain = f"gui/{os.getuid()}"
            if action == "enable":
                _atomic(target, template.read_bytes())
                self._manager(["launchctl", "enable", f"{domain}/{LABEL}"])
                loaded = self._manager(
                    ["launchctl", "print", f"{domain}/{LABEL}"], required=False
                )
                if not loaded:
                    self._manager(["launchctl", "bootstrap", domain, str(target)])
            else:
                if self._manager(
                    ["launchctl", "print", f"{domain}/{LABEL}"], required=False
                ):
                    self._manager(["launchctl", "bootout", f"{domain}/{LABEL}"])
                target.unlink(missing_ok=True)
        else:
            target = Path.home() / ".config/systemd/user" / f"{LABEL}.service"
            if action == "enable":
                _atomic(target, template.read_bytes())
                self._manager(["systemctl", "--user", "daemon-reload"])
                self._manager(["systemctl", "--user", "enable", "--now", target.name])
            else:
                if target.exists():
                    self._manager(
                        ["systemctl", "--user", "disable", "--now", target.name]
                    )
                    target.unlink()
                    self._manager(["systemctl", "--user", "daemon-reload"])
        return {
            "enabled": action == "enable",
            "delivery_state_preserved": True,
            **self.status(),
        }

    @staticmethod
    def _manager(command: list[str], *, required: bool = True) -> bool:
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=45, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("collector_supervisor_unavailable") from None
        if required and result.returncode:
            raise RuntimeError("collector_supervisor_operation_failed")
        return result.returncode == 0


def handle_service(args) -> dict[str, Any]:
    service = HostService()
    action = args.service_action
    if action == "add":
        profile = getattr(args, "credential_profile", None) or os.environ.get(
            "CT_CREDENTIAL_PROFILE"
        )
        if not profile:
            raise ValueError("select a collector connection with --profile")
        return service.add(
            name=args.name,
            profile=profile,
            directory=Path(args.directory),
            project_name=args.project_name,
            state_path=Path(args.state_path) if args.state_path else None,
            agent_vendor=args.agent_vendor,
        )
    if action == "remove":
        return service.remove(args.name)
    if action == "install":
        return service.install(
            secret_file=Path(args.secret_file) if args.secret_file else None
        )
    if action == "enable":
        if args.automatic:
            service.policy(mode="automatic")
        return service.supervisor("enable")
    if action == "disable":
        return service.supervisor("disable")
    if action == "policy":
        return service.policy(mode=args.mode, project_name=args.project)
    if action in {"pause", "resume"}:
        return service.policy(paused=action == "pause", project_name=args.project)
    if action == "run":
        return service.run()
    return service.status()


def register_service(commands) -> None:
    import argparse

    from coding_trajectory_cli._shared import add_agent_vendor_flag

    service = commands.add_parser(
        "service", help="Manage one collector service for this host."
    )
    service.set_defaults(_plugin_handler=handle_service, _default_output="json")
    actions = service.add_subparsers(dest="service_action", required=True)
    add = actions.add_parser(
        "add", help="Register a project without enabling publication."
    )
    add.add_argument("name")
    add.add_argument("--profile", dest="credential_profile", default=argparse.SUPPRESS)
    add.add_argument("--directory", default=".")
    add.add_argument("--project-name")
    add.add_argument("--state-path")
    add_agent_vendor_flag(add)
    remove = actions.add_parser(
        "remove", help="Remove a stopped project's registration; retain its data."
    )
    remove.add_argument("name")
    install = actions.add_parser(
        "install", help="Write a supervisor template without enabling it."
    )
    install.add_argument(
        "--secret-file",
        help="Private JSON mapping of injected token environment names to values.",
    )
    enable = actions.add_parser(
        "enable",
        help="Explicitly enable host supervision, retaining publication policy.",
    )
    enable.add_argument(
        "--automatic",
        action="store_true",
        help="Explicitly select automatic publication for registered projects.",
    )
    actions.add_parser("disable", help="Stop supervision and preserve delivery state.")
    actions.add_parser(
        "status",
        help="Inspect local service and all project queues without network access.",
    )
    actions.add_parser(
        "run",
        help="Run the host service in the foreground (normally started by its supervisor).",
    )
    policy = actions.add_parser(
        "policy", help="Persist publication mode for all or one registered project."
    )
    policy.add_argument("--mode", choices=("manual", "automatic"), required=True)
    policy.add_argument("--project")
    for action in ("pause", "resume"):
        command = actions.add_parser(
            action, help=f"{action.title()} delivery without changing publication mode."
        )
        command.add_argument("--project")
