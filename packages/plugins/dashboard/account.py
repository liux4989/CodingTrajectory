"""Dashboard-owned account identity inference.

The ``AccountIdentity`` model itself lives in core
(``coding_trajectory.ingestion.models``) because the ``Session`` model
references it. This module owns the *inference heuristics* that extract an
account identity from raw vendor payloads, plus a convenience re-export of
``AccountIdentity`` so dashboard callers can import everything from here.
"""

from __future__ import annotations

from typing import Any

from coding_trajectory.ingestion.models import AccountIdentity


__all__ = ["AccountIdentity", "infer_account_identity"]


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
