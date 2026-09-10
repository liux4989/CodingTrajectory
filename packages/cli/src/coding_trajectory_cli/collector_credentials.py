"""Private, scoped Cloudflare credentials for the host-local collector."""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import keyring
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KEYCHAIN_SERVICE_PREFIX = "CodingTrajectory collector credentials v2"


class CollectorCredentialError(RuntimeError):
    """A private collector profile could not be loaded."""


class CollectorCredentialProfile(BaseModel):
    """Profile settings; tokens stay in Keychain or the process environment."""

    model_config = ConfigDict(extra="forbid")

    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    version: Literal[2] = 2
    cloudflare_url: HttpUrl
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CollectorCredentials:
    profile: CollectorCredentialProfile
    access_token: str


def profile_path(profile_name: str) -> Path:
    _validate_profile_name(profile_name)
    return (
        Path("~/.coding-trajectory/control-plane/credentials").expanduser()
        / f"{profile_name}.json"
    )


def configure_profile(
    *,
    profile_name: str,
    cloudflare_url: str,
    token: str | None,
    workspace_id: UUID,
    agent_id: UUID,
    project_id: UUID | None,
    token_env: str | None = None,
) -> CollectorCredentialProfile:
    if token_env is None:
        _require_macos_keychain()
    if token_env is None and not token:
        raise CollectorCredentialError("collector token must not be empty")
    profile = CollectorCredentialProfile(
        token_env=token_env,
        cloudflare_url=cloudflare_url,
        workspace_id=workspace_id,
        agent_id=agent_id,
        project_id=project_id,
    )
    if token_env is None:
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
    present = bool(_profile_token(profile_name, profile))
    return {
        "profile": profile_name,
        "configured": True,
        "token_storage": "environment" if profile.token_env else "macOS Keychain",
        "token_present": present,
        "keychain_token_present": present if not profile.token_env else False,
        "workspace_configured": True,
        "agent_configured": True,
    }


def _profile_token(
    profile_name: str, profile: CollectorCredentialProfile
) -> str | None:
    if profile.token_env:
        return os.environ.get(profile.token_env)
    _require_macos_keychain()
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
    backend = keyring.get_keyring()
    if not backend.__class__.__module__.startswith("keyring.backends.macOS"):
        raise CollectorCredentialError(
            "macOS Keychain backend is unavailable; refusing insecure credential storage"
        )
