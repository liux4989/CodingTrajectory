"""Private, refreshable credentials for the host-local collector."""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import keyring
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KEYCHAIN_SERVICE_PREFIX = "CodingTrajectory collector credentials v1"


class CollectorCredentialError(RuntimeError):
    """A private collector profile could not be loaded or refreshed."""


class CollectorCredentialProfile(BaseModel):
    """Profile settings; passwords stay in Keychain or the process environment."""

    model_config = ConfigDict(extra="forbid")

    password_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    version: int = 1
    supabase_url: HttpUrl
    supabase_api_key: str
    email: str
    workspace_id: UUID
    agent_id: UUID
    project_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RefreshedCollectorCredentials:
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
    supabase_url: str,
    supabase_api_key: str,
    email: str,
    password: str | None,
    workspace_id: UUID,
    agent_id: UUID,
    project_id: UUID | None,
    password_env: str | None = None,
) -> CollectorCredentialProfile:
    if password_env is None:
        _require_macos_keychain()
    if password_env is None and not password:
        raise CollectorCredentialError("collector password must not be empty")
    profile = CollectorCredentialProfile(
        password_env=password_env,
        supabase_url=supabase_url,
        supabase_api_key=supabase_api_key,
        email=email,
        workspace_id=workspace_id,
        agent_id=agent_id,
        project_id=project_id,
    )
    if password_env is None:
        keyring.set_password(_keychain_service(profile_name), "password", password)
    _write_profile(profile_name, profile)
    return profile


def refresh_profile(profile_name: str) -> RefreshedCollectorCredentials:
    profile = _read_profile(profile_name)
    password = _profile_password(profile_name, profile)
    if not password:
        raise CollectorCredentialError(
            "collector password is unavailable in the configured secret backend"
        )
    payload = json.dumps({"email": profile.email, "password": password}).encode()
    request = urllib.request.Request(
        f"{str(profile.supabase_url).rstrip('/')}/auth/v1/token?grant_type=password",
        data=payload,
        headers={
            "apikey": profile.supabase_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise CollectorCredentialError("collector credential refresh failed") from exc
    access_token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(access_token, str) or access_token.count(".") != 2:
        raise CollectorCredentialError(
            "collector credential refresh returned no access token"
        )
    return RefreshedCollectorCredentials(profile=profile, access_token=access_token)


def profile_summary(profile_name: str) -> dict[str, Any]:
    profile = _read_profile(profile_name)
    present = bool(_profile_password(profile_name, profile))
    return {
        "profile": profile_name,
        "configured": True,
        "password_storage": "environment" if profile.password_env else "macOS Keychain",
        "password_present": present,
        "keychain_password_present": present if not profile.password_env else False,
        "workspace_configured": True,
        "agent_configured": True,
    }


def _profile_password(
    profile_name: str, profile: CollectorCredentialProfile
) -> str | None:
    if profile.password_env:
        return os.environ.get(profile.password_env)
    _require_macos_keychain()
    return keyring.get_password(_keychain_service(profile_name), "password")


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
    return CollectorCredentialProfile.model_validate(raw)


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
