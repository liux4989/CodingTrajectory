"""Multicodex-powered Codex account resolution for the dashboard plugin.

Ports the essential state + JWT + refresh logic from the sibling
``mxc`` CLI (``labs/multicodex``) without taking a runtime dependency on it.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


MXC_DIR = Path.home() / ".mxc"
STORE_FILE = MXC_DIR / "state.json"
DEFAULT_CODEX_HOME = Path.home() / ".codex"

OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
DEFAULT_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_THRESHOLD_SECONDS = 300


def load_mxc_state() -> dict:
    """Read ``~/.mxc/state.json`` if present; return an empty state otherwise."""
    if not STORE_FILE.exists():
        return {"profiles": {}, "active": None}
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"profiles": {}, "active": None}
    if not isinstance(data, dict):
        return {"profiles": {}, "active": None}
    data.setdefault("profiles", {})
    data.setdefault("active", None)
    return data


def save_mxc_state(data: dict) -> None:
    MXC_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_name(f".{STORE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE_FILE)


def load_default_codex_auth() -> dict | None:
    """Read ``~/.codex/auth.json`` for users who don't run multicodex."""
    path = DEFAULT_CODEX_HOME / "auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def jwt_payload(token: str | None) -> dict | None:
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def token_needs_refresh(token: str | None, threshold: int = TOKEN_REFRESH_THRESHOLD_SECONDS) -> bool:
    claims = jwt_payload(token)
    if not claims:
        return False
    exp = claims.get("exp")
    if not isinstance(exp, int):
        return False
    return exp - int(time.time()) <= threshold


def auth_mode_from_profile(profile_auth: dict | None) -> str:
    if not isinstance(profile_auth, dict):
        return "unknown"
    tokens = profile_auth.get("tokens", {}) or {}
    access_token = tokens.get("access_token")
    if isinstance(access_token, str) and access_token.startswith("at-"):
        return "personalAccessToken"
    if isinstance(profile_auth.get("OPENAI_API_KEY"), str):
        return "apiKey"
    if isinstance(access_token, str) and access_token:
        return "chatgpt"
    return "unknown"


def refresh_profile_auth(account: str, profile_auth: dict) -> dict:
    """Exchange the stored refresh token for a new access/id token set.

    Persists the refreshed payload back into ``~/.mxc/state.json`` so later
    calls see the new tokens.
    """
    tokens = profile_auth.get("tokens", {}) or {}
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError(f"Missing refresh token for account '{account}'")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.environ.get("CODEX_CLIENT_ID", DEFAULT_CODEX_CLIENT_ID),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_TOKEN_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "coding-trajectory-dashboard/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise RuntimeError(f"Refresh response missing access token for account '{account}'")

    refreshed = json.loads(json.dumps(profile_auth))
    refreshed_tokens = dict(refreshed.get("tokens", {}) or {})
    refreshed_tokens["access_token"] = new_access.strip()
    for key in ("refresh_token", "id_token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            refreshed_tokens[key] = value.strip()
    refreshed["tokens"] = refreshed_tokens

    state = load_mxc_state()
    profiles = state.get("profiles", {})
    if account in profiles and isinstance(profiles[account], dict):
        profiles[account]["auth"] = refreshed
        state["profiles"] = profiles
        save_mxc_state(state)
    return refreshed


def account_info_from_jwt(profile_auth: dict | None) -> dict[str, Any] | None:
    """Extract ``{email, planType}`` from the stored ``id_token`` JWT."""
    if not isinstance(profile_auth, dict):
        return None
    tokens = profile_auth.get("tokens", {}) or {}
    claims = jwt_payload(tokens.get("id_token"))
    if not claims:
        return None
    info: dict[str, Any] = {}
    email = claims.get("email")
    if isinstance(email, str) and email:
        info["email"] = email
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        plan = auth_claim.get("chatgpt_plan_type")
        if isinstance(plan, str) and plan:
            info["planType"] = plan
    return info or None


def maybe_refresh_access_token(account: str, profile_auth: dict) -> dict:
    """Refresh the access token if it's within the expiry threshold."""
    tokens = profile_auth.get("tokens", {}) or {}
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return profile_auth
    if not token_needs_refresh(access_token):
        return profile_auth
    if isinstance(access_token, str) and access_token.startswith("at-"):
        return profile_auth
    return refresh_profile_auth(account, profile_auth)
