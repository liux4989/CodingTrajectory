"""Dashboard-owned account identity resolution.

Provides a real-API Codex resolver (``resolve_codex_account``) that uses
the multicodex state store and the Codex ``app-server`` JSON-RPC, plus a
payload heuristic (``infer_account_identity``) for vendors without a live
API path.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel


__all__ = [
    "AccountIdentity",
    "infer_account_identity",
    "resolve_codex_account",
]


logger = logging.getLogger(__name__)


class AccountIdentity(BaseModel):
    key: str
    label: str | None = None
    vendor: str | None = None
    plan_type: str | None = None
    auth_mode: str | None = None


_ACCOUNT_CANDIDATE_KEYS = (
    "account_key",
    "accountKey",
    "account_id",
    "accountId",
    "account_email",
    "accountEmail",
    "email",
    "user_email",
    "userEmail",
    "username",
    "login",
    "user_id",
    "userId",
)
_ACCOUNT_CONTAINER_KEYS = ("account", "user", "owner", "profile", "auth")


def _as_vendor_string(vendor: Any) -> str:
    value = getattr(vendor, "value", vendor)
    if not isinstance(value, str):
        raise TypeError(f"vendor must be str or Vendor enum, got {type(vendor).__name__}")
    return value


def resolve_codex_account(
    *,
    profile: str | None = None,
    timeout: int = 20,
) -> AccountIdentity | None:
    """Resolve a Codex account via the multicodex state store + live RPC.

    Reads ``~/.mxc/state.json``, refreshes the OAuth access token when it is
    close to expiry, spawns ``codex app-server --stdio`` to issue
    ``account/read``, and returns an ``AccountIdentity`` keyed by the
    account's email address.

    Falls back to decoding ``~/.codex/auth.json``'s ``id_token`` JWT when
    multicodex isn't installed or no profile has usable tokens.
    """
    try:
        from . import codex_rpc
        from .codex_auth import load_mxc_state, maybe_refresh_access_token
    except ImportError:
        import codex_rpc
        from codex_auth import load_mxc_state, maybe_refresh_access_token

    state = load_mxc_state()
    profiles = state.get("profiles", {}) or {}
    active = state.get("active")
    ordered = _ordered_profiles(profiles, active, profile)

    for name in ordered:
        entry = profiles.get(name)
        profile_auth = entry.get("auth") if isinstance(entry, dict) else None
        if not isinstance(profile_auth, dict):
            continue
        try:
            profile_auth = maybe_refresh_access_token(name, profile_auth)
            snapshot = codex_rpc.account_snapshot(
                name, profile_auth, persist_runtime_auth=True
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.debug("codex account/read failed for %s: %s", name, exc)
            continue
        info = codex_rpc.extract_account_info(snapshot, profile_auth)
        if not info or "email" not in info:
            continue
        return AccountIdentity(
            key=info["email"],
            label=name,
            vendor="codex_cli",
            plan_type=info.get("planType"),
            auth_mode=info.get("authMode"),
        )

    return _resolve_default_codex_auth()


def _ordered_profiles(
    profiles: dict, active: str | None, preferred: str | None
) -> list[str]:
    names = list(profiles.keys())
    order: list[str] = []
    if preferred and preferred in names:
        order.append(preferred)
    if active and active in names and active not in order:
        order.append(active)
    for name in sorted(names):
        if name not in order:
            order.append(name)
    return order


def _resolve_default_codex_auth() -> AccountIdentity | None:
    """Fallback for users who run Codex without multicodex."""
    try:
        from . import codex_auth
    except ImportError:
        import codex_auth

    profile_auth = codex_auth.load_default_codex_auth()
    if not profile_auth:
        return None
    info = codex_auth.account_info_from_jwt(profile_auth)
    if not info or "email" not in info:
        return None
    return AccountIdentity(
        key=info["email"],
        label="default",
        vendor="codex_cli",
        plan_type=info.get("planType"),
        auth_mode=codex_auth.auth_mode_from_profile(profile_auth),
    )


def infer_account_identity(
    raw: Any, *, vendor: Any, max_depth: int = 2
) -> AccountIdentity | None:
    """Heuristic account extraction from a raw vendor payload dict.

    Use ``resolve_codex_account`` for Codex -- this helper exists for
    vendors that don't expose a live account endpoint.
    """
    if not isinstance(raw, dict) or max_depth < 0:
        return None

    value = _mapping_account_value(raw)
    if isinstance(value, str):
        label = _mapping_account_label(raw)
        return AccountIdentity(
            key=value, label=label or value, vendor=_as_vendor_string(vendor)
        )

    for key in _ACCOUNT_CONTAINER_KEYS:
        nested = raw.get(key)
        if isinstance(nested, dict):
            account = infer_account_identity(nested, vendor=vendor, max_depth=max_depth - 1)
            if account is not None:
                return account

    for value in raw.values():
        if isinstance(value, dict):
            account = infer_account_identity(value, vendor=vendor, max_depth=max_depth - 1)
            if account is not None:
                return account

    return None


def _mapping_account_value(raw: dict[str, Any]) -> str | None:
    for key in _ACCOUNT_CANDIDATE_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _mapping_account_label(raw: dict[str, Any]) -> str | None:
    for key in ("label", "display_name", "displayName", "name"):
        value = raw.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None
