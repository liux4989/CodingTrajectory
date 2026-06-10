"""Base adapter interface for agent ingestion."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from coding_trajectory.ingestion.models import AccountIdentity, Session, Vendor

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


class BaseAdapter(ABC):
    vendor: Vendor
    _file_glob: str = "*.jsonl"

    def _reset_ingest_state(self) -> None:
        pass

    def _load_records(self, path: Path) -> list[dict]:
        records: list[dict] = []
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
        return records

    def ingest_file(self, path: Path) -> Session:
        self._reset_ingest_state()
        records = self._load_records(path)
        return self._build_session(path, records)

    def ingest_directory(self, directory: Path) -> list[Session]:
        sessions: list[Session] = []
        for source_file in sorted(directory.glob(self._file_glob)):
            sessions.append(self.ingest_file(source_file))
        return sessions

    def ingest(self, source: Path) -> Session:
        return self.ingest_file(source)

    @abstractmethod
    def _build_session(self, source: Path, records: list[dict]) -> Session:
        ...


def infer_account_identity(
    raw: Any, *, vendor: Vendor, max_depth: int = 2
) -> AccountIdentity | None:
    """Vendor-agnostic account identity extraction.

    The dashboard plugin keeps a parallel copy of this logic in
    ``packages/plugins/dashboard/account.py`` for dashboard-side consumers
    that operate on raw vendor payloads without loading the ingestion
    pipeline.
    """
    if not isinstance(raw, dict) or max_depth < 0:
        return None

    value = _mapping_account_value(raw)
    if isinstance(value, str):
        label = _mapping_account_label(raw)
        return AccountIdentity(key=value, label=label or value, vendor=vendor.value)

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
