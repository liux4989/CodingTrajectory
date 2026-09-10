"""Private, scoped Cloudflare credentials for the host-local collector."""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from coding_trajectory.control_plane.remote import cloudflare_endpoint

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KEYCHAIN_SERVICE_PREFIX = "CodingTrajectory collector credentials v2"


class CollectorCredentialError(RuntimeError):
    """A private collector profile could not be loaded."""


class CollectorCredentialProfile(BaseModel):
    """Profile settings; tokens stay in Keychain or the process environment."""

    model_config = ConfigDict(extra="forbid")

    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    version: Literal[2, 3] = 3
    cloudflare_url: HttpUrl | None = None
    workspace_id: UUID | None = None
    agent_id: UUID | None = None
    project_id: UUID | None = None
    project_name: str | None = Field(default=None, min_length=1, max_length=256)
    state_path: str | None = None
    default_source: Literal["local", "shared", "auto"] = "auto"
    role: Literal["collector", "reader", "local"] = "collector"

    @model_validator(mode="after")
    def validate_connection(self):
        if self.role == "local":
            if self.default_source != "local" or any(
                (self.cloudflare_url, self.workspace_id, self.agent_id, self.token_env)
            ):
                raise ValueError(
                    "local profiles require local source and no remote identity"
                )
        elif not self.cloudflare_url or not self.workspace_id:
            raise ValueError("remote profiles require endpoint and workspace")
        elif self.role == "collector" and not self.agent_id:
            raise ValueError("collector profiles require agent identity")
        if self.cloudflare_url:
            cloudflare_endpoint(str(self.cloudflare_url))
        return self


@dataclass(frozen=True, slots=True)
class CollectorCredentials:
    profile: CollectorCredentialProfile
    access_token: str = field(repr=False)


def profile_path(profile_name: str) -> Path:
    _validate_profile_name(profile_name)
    return (
        Path(
            os.environ.get(
                "CT_CONNECTION_DIR", "~/.coding-trajectory/control-plane/credentials"
            )
        ).expanduser()
        / f"{profile_name}.json"
    )


def configure_profile(
    *,
    profile_name: str,
    cloudflare_url: str | None,
    token: str | None,
    workspace_id: UUID | None,
    agent_id: UUID | None,
    project_id: UUID | None,
    token_env: str | None = None,
    project_name: str | None = None,
    state_path: str | None = None,
    default_source: Literal["local", "shared", "auto"] = "auto",
    role: Literal["collector", "reader", "local"] = "collector",
) -> CollectorCredentialProfile:
    _validate_profile_name(profile_name)
    if token_env is None and role != "local":
        _require_macos_keychain()
    if token_env is None and not token and role != "local":
        raise CollectorCredentialError("collector token must not be empty")
    profile = CollectorCredentialProfile(
        token_env=token_env,
        cloudflare_url=cloudflare_url,
        workspace_id=workspace_id,
        agent_id=agent_id,
        project_id=project_id,
        project_name=project_name,
        state_path=state_path,
        default_source=default_source,
        role=role,
    )
    if token_env is None and role != "local":
        import keyring

        keyring.set_password(_keychain_service(profile_name), "token", token)
    _write_profile(profile_name, profile)
    return profile


def load_profile_credentials(profile_name: str) -> CollectorCredentials:
    profile = _read_profile(profile_name)
    token = _profile_token(profile_name, profile)
    if not token:
        raise CollectorCredentialError(
            "collector token is unavailable in the configured secret backend"
        )
    return CollectorCredentials(profile=profile, access_token=token)


def profile_summary(profile_name: str) -> dict[str, Any]:
    profile = _read_profile(profile_name)
    try:
        present = bool(_profile_token(profile_name, profile))
    except CollectorCredentialError:
        present = False
    return {
        "profile": profile_name,
        "configured": True,
        "token_storage": "none"
        if profile.role == "local"
        else "environment"
        if profile.token_env
        else "macOS Keychain",
        "role": profile.role,
        "default_source": profile.default_source,
        "token_present": present,
        "keychain_token_present": present if not profile.token_env else False,
        "workspace_configured": profile.workspace_id is not None,
        "agent_configured": profile.agent_id is not None,
    }


def _profile_token(
    profile_name: str, profile: CollectorCredentialProfile
) -> str | None:
    if profile.role == "local":
        return None
    if profile.token_env:
        return os.environ.get(profile.token_env)
    _require_macos_keychain()
    import keyring

    return keyring.get_password(_keychain_service(profile_name), "token")


def _read_profile(profile_name: str) -> CollectorCredentialProfile:
    path = profile_path(profile_name)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CollectorCredentialError(
            f"collector profile {profile_name!r} is not configured; run credentials configure"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorCredentialError(
            f"collector profile {profile_name!r} is unreadable"
        ) from exc
    try:
        return CollectorCredentialProfile.model_validate(raw)
    except ValueError:
        raise CollectorCredentialError(
            "collector profile is invalid; configure a Cloudflare token profile"
        ) from None


def _write_profile(profile_name: str, profile: CollectorCredentialProfile) -> None:
    path = profile_path(profile_name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    encoded = (
        json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{profile_name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _keychain_service(profile_name: str) -> str:
    _validate_profile_name(profile_name)
    return f"{_KEYCHAIN_SERVICE_PREFIX}:{profile_name}"


def _validate_profile_name(profile_name: str) -> None:
    if not _PROFILE_PATTERN.fullmatch(profile_name):
        raise CollectorCredentialError(
            "profile must contain only letters, numbers, dot, underscore, or hyphen"
        )


def _require_macos_keychain() -> None:
    if platform.system() != "Darwin":
        raise CollectorCredentialError(
            "secure collector profiles currently require macOS Keychain"
        )
    try:
        import keyring
    except ImportError:
        raise CollectorCredentialError(
            "Keychain integration requires the ct CLI package"
        ) from None
    backend = keyring.get_keyring()
    if not backend.__class__.__module__.startswith("keyring.backends.macOS"):
        raise CollectorCredentialError(
            "macOS Keychain backend is unavailable; refusing insecure credential storage"
        )


# Public metadata loader deliberately does not resolve secrets.
load_profile = _read_profile
ConnectionProfile = CollectorCredentialProfile
ConnectionError = CollectorCredentialError


def rotate_profile(
    profile_name: str, *, token: str | None = None, token_env: str | None = None
) -> CollectorCredentialProfile:
    profile = load_profile(profile_name)
    if profile.role == "local":
        raise ConnectionError("local profiles have no credential to rotate")
    return configure_profile(
        profile_name=profile_name,
        cloudflare_url=str(profile.cloudflare_url),
        workspace_id=profile.workspace_id,
        agent_id=profile.agent_id,
        project_id=profile.project_id,
        project_name=profile.project_name,
        state_path=profile.state_path,
        role=profile.role,
        default_source=profile.default_source,
        token=token,
        token_env=token_env,
    )


def forget_profile(profile_name: str) -> None:
    profile = load_profile(profile_name)
    if not profile.token_env and profile.role != "local":
        _require_macos_keychain()
        import keyring

        try:
            keyring.delete_password(_keychain_service(profile_name), "token")
        except keyring.errors.PasswordDeleteError:
            pass
    profile_path(profile_name).unlink()


def resolve_credentials(
    *,
    profile_name: str | None = None,
    url: str | None = None,
    access_token: str | None = None,
    workspace_id: UUID | str | None = None,
) -> CollectorCredentials:
    selected = profile_name or os.environ.get("CT_CREDENTIAL_PROFILE")
    if selected:
        credentials = load_profile_credentials(selected)
        if url and url.rstrip("/") != str(credentials.profile.cloudflare_url).rstrip(
            "/"
        ):
            raise ConnectionError(
                "endpoint override conflicts with selected connection profile"
            )
        if workspace_id and str(workspace_id) != str(credentials.profile.workspace_id):
            raise ConnectionError(
                "workspace override conflicts with selected connection profile"
            )
        if access_token:
            raise ConnectionError(
                "token override cannot be combined with a selected profile"
            )
        return credentials
    endpoint = url or os.environ.get("CT_CLOUDFLARE_URL")
    token = access_token or os.environ.get("CT_ACCESS_TOKEN")
    workspace = workspace_id or os.environ.get("CT_REMOTE_WORKSPACE_ID")
    if any((endpoint, token, workspace)):
        if not all((endpoint, token, workspace)):
            raise ConnectionError(
                "remote connection requires complete endpoint, token, and workspace configuration"
            )
        try:
            profile = ConnectionProfile(
                cloudflare_url=endpoint,
                workspace_id=workspace,
                role="reader",
                default_source="shared",
            )
        except ValueError:
            raise ConnectionError(
                "remote connection configuration is invalid"
            ) from None
        return CollectorCredentials(profile=profile, access_token=token)
    return load_profile_credentials("default")


def query_source(*, source: str | None = None, profile_name: str | None = None) -> str:
    selected_source = source or os.environ.get("CT_QUERY_SOURCE")
    if selected_source is not None:
        if selected_source not in {"local", "shared", "auto"}:
            raise ConnectionError("query source must be local, shared, or auto")
        return selected_source
    selected = profile_name or os.environ.get("CT_CREDENTIAL_PROFILE")
    if selected:
        return load_profile(selected).default_source
    if profile_path("default").exists() and not os.environ.get("CT_CLOUDFLARE_URL"):
        return load_profile("default").default_source
    return "auto"
