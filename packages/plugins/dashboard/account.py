"""Dashboard-owned account identity inference.

Defines ``AccountIdentity`` locally and provides inference heuristics that
extract an account identity from raw vendor payloads. This module must not
import from core: core depends on the plugins package, so any core→plugins
import chain that also reached back into core would create a circular
import at module load time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


__all__ = ["AccountIdentity", "infer_account_identity"]


class AccountIdentity(BaseModel):
    key: str
    label: str | None = None
    vendor: str | None = None


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


def infer_account_identity(
    raw: Any, *, vendor: Any, max_depth: int = 2
) -> AccountIdentity | None:
    """Extract an ``AccountIdentity`` from a vendor payload.

    ``vendor`` may be a core ``Vendor`` enum value or a plain string tag.
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
