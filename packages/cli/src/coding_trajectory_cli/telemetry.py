"""Telemetry configuration and invocation log helpers for the CLI."""

from __future__ import annotations

import datetime as _dt
import fcntl
import os
import tempfile
import tomllib
from collections.abc import Mapping
from datetime import timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

TELEMETRY_ENV_VAR = "CT_TELEMETRY"
TELEMETRY_DISABLED_VALUES = {"0", "false", "no", "off"}
TELEMETRY_ENABLED_VALUES = {"1", "true", "yes", "on"}
TELEMETRY_DIRNAME = ".coding-trajectory"
CONFIG_FILENAME = "config.toml"
INVOCATION_LOG_FILENAME = "invocations.jsonl"
INVOCATION_LOG_LOCK_SUFFIX = ".lock"
INVOCATION_MAX_AGE_DAYS = 30
INVOCATION_MAX_BYTES = 10 * 1024 * 1024


class TelemetryConfigIssue(BaseModel):
    """Structured config parsing issue that Doctor can surface directly."""

    kind: Literal["read_error", "toml_decode_error", "validation_error"]
    path: str
    message: str


class TelemetryDecision(BaseModel):
    """Resolved telemetry decision with explicit fallback metadata."""

    enabled: bool
    source: Literal["env", "config", "default"]
    detail: str
    env_value: str | None = None
    config_issue: TelemetryConfigIssue | None = None


class TelemetrySection(BaseModel):
    """Config surface under ``[telemetry]``."""

    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None


class TelemetryFileConfig(BaseModel):
    """Root config file model."""

    model_config = ConfigDict(extra="ignore")

    telemetry: TelemetrySection | None = None


class InvocationWarningRecord(BaseModel):
    """One structured warning captured during an invocation."""

    model_config = ConfigDict(extra="allow")

    message: str
    code: str | None = None
    severity: Literal["info", "warning", "error"] = "warning"
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must be a non-empty string")
        return value


class InvocationRecord(BaseModel):
    """One newline-delimited invocation record."""

    model_config = ConfigDict(extra="allow")

    ts: _dt.datetime
    ct_version: str | None = None
    cwd: str
    cmd: str
    method: str | None = None
    session_id: str | None = None
    vendor: str | None = None
    exit_code: StrictInt = Field(default=0, ge=0)
    ok: StrictBool
    error: str | None = None
    ms: StrictInt = Field(ge=0)
    warnings: list[InvocationWarningRecord] = Field(default_factory=list)

    @field_validator("ts")
    @classmethod
    def _validate_timestamp(cls, value: _dt.datetime) -> _dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_validator("ts", mode="before")
    @classmethod
    def _validate_timestamp_input(cls, value: Any) -> Any:
        if not isinstance(value, (str, _dt.datetime)):
            raise TypeError("timestamp must be an ISO-8601 string")
        return value

    @field_validator("cwd", "cmd")
    @classmethod
    def _validate_non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be a non-empty string")
        return value


def telemetry_config_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / TELEMETRY_DIRNAME / CONFIG_FILENAME


def invocation_log_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / TELEMETRY_DIRNAME / INVOCATION_LOG_FILENAME


def _normalize_env_value(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    return normalized or None


def _validation_issue_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for issue in exc.errors():
        location = ".".join(str(part) for part in issue.get("loc", ())) or "<root>"
        parts.append(f"{location}: {issue.get('msg', 'invalid value')}")
    return "; ".join(parts)


def resolve_telemetry_decision(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> TelemetryDecision:
    """Resolve telemetry enablement with explicit fallback metadata."""

    env = os.environ if environ is None else environ
    env_value = _normalize_env_value(env.get(TELEMETRY_ENV_VAR))
    if env_value is not None:
        if env_value in TELEMETRY_DISABLED_VALUES:
            return TelemetryDecision(
                enabled=False,
                source="env",
                detail=f"disabled via {TELEMETRY_ENV_VAR}",
                env_value=env_value,
            )
        detail = f"enabled via {TELEMETRY_ENV_VAR}"
        if env_value not in TELEMETRY_ENABLED_VALUES:
            detail = f"enabled via {TELEMETRY_ENV_VAR}={env_value}"
        return TelemetryDecision(
            enabled=True,
            source="env",
            detail=detail,
            env_value=env_value,
        )

    config_path = telemetry_config_path(home)
    if config_path.exists():
        try:
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            config = TelemetryFileConfig.model_validate(parsed)
        except OSError as exc:
            return TelemetryDecision(
                enabled=True,
                source="default",
                detail="enabled by default",
                config_issue=TelemetryConfigIssue(
                    kind="read_error",
                    path=str(config_path),
                    message=str(exc),
                ),
            )
        except tomllib.TOMLDecodeError as exc:
            return TelemetryDecision(
                enabled=True,
                source="default",
                detail="enabled by default",
                config_issue=TelemetryConfigIssue(
                    kind="toml_decode_error",
                    path=str(config_path),
                    message=str(exc),
                ),
            )
        except ValidationError as exc:
            return TelemetryDecision(
                enabled=True,
                source="default",
                detail="enabled by default",
                config_issue=TelemetryConfigIssue(
                    kind="validation_error",
                    path=str(config_path),
                    message=_validation_issue_message(exc),
                ),
            )

        if config.telemetry and config.telemetry.enabled is not None:
            enabled = config.telemetry.enabled
            return TelemetryDecision(
                enabled=enabled,
                source="config",
                detail="enabled in config.toml" if enabled else "disabled in config.toml",
            )

    return TelemetryDecision(enabled=True, source="default", detail="enabled by default")


def write_invocation_record(
    record: InvocationRecord | dict[str, Any],
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Best-effort write with coordinated age/size rotation."""

    decision = resolve_telemetry_decision(home=home, environ=environ)
    if not decision.enabled:
        return
    try:
        invocation = record if isinstance(record, InvocationRecord) else InvocationRecord.model_validate(record)
    except ValidationError:
        return

    path = invocation_log_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}{INVOCATION_LOG_LOCK_SUFFIX}")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                line = invocation.model_dump_json() + "\n"
                if len(line.encode("utf-8")) > INVOCATION_MAX_BYTES:
                    return
                if _prepare_log_for_append_locked(
                    path=path,
                    incoming_line=line,
                    now=invocation.ts,
                ):
                    _append_line_locked(path, line)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, UnicodeError):
        return


def _coerce_utc(value: _dt.datetime) -> _dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc)


def _append_line_locked(path: Path, line: str) -> None:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, line.encode("utf-8"))
    except OSError:
        return
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _prepare_log_for_append_locked(
    *,
    path: Path,
    incoming_line: str,
    now: _dt.datetime,
) -> bool:
    """Prepare a valid log for append without deleting corruption evidence."""

    if not path.exists():
        return True

    cutoff = _coerce_utc(now) - _dt.timedelta(days=INVOCATION_MAX_AGE_DAYS)
    incoming_size = len(incoming_line.encode("utf-8"))
    retained: list[str] = []
    retained_sizes: list[int] = []
    total_size = 0
    rewrite_needed = False
    corrupt = False

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line:
                rewrite_needed = True
                continue
            try:
                record = InvocationRecord.model_validate_json(line)
            except ValidationError:
                corrupt = True
                continue
            if _coerce_utc(record.ts) < cutoff:
                rewrite_needed = True
                continue
            serialized = record.model_dump_json()
            retained.append(serialized)
            line_size = len(serialized.encode("utf-8")) + 1
            retained_sizes.append(line_size)
            total_size += line_size

    if corrupt:
        return path.stat().st_size + incoming_size <= INVOCATION_MAX_BYTES

    while retained and total_size + incoming_size > INVOCATION_MAX_BYTES:
        rewrite_needed = True
        total_size -= retained_sizes.pop(0)
        retained.pop(0)

    if not rewrite_needed:
        return True

    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_handle:
            for line in retained:
                tmp_handle.write(line)
                tmp_handle.write("\n")
            tmp_handle.flush()
            os.fsync(tmp_handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return False
    return True
