"""Doctor command: diagnostic surface for environment and invocation telemetry."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from coding_trajectory_cli._shared import GhFormatter
from coding_trajectory_cli.outcome import CommandOutcome
from coding_trajectory_cli.telemetry import (
    InvocationRecord,
    InvocationWarningRecord as InvocationWarning,
    invocation_log_path,
    resolve_telemetry_decision,
)

_CACHE_DIR = Path.home() / ".coding-trajectory"
_INDEX_CACHE_PATH = _CACHE_DIR / "index.json"
_SUPPORTED_SINCE_VALUES = ("7d", "30d", "90d", "all")
_MARKDOWN_FAILURE_LIMIT = 10
_MARKDOWN_WARNING_LIMIT = 10
_MARKDOWN_TREND_LIMIT = 20
_MARKDOWN_STALE_LIMIT = 20
_ENVIRONMENT_CHECK_ORDER = (
    "python_version",
    "cli_version",
    "config_files",
    "telemetry",
    "index_cache",
    "vendor_root_codex",
    "vendor_root_claude",
    "vendor_root_pi",
)
_ENVIRONMENT_CHECK_LABELS = {
    "python_version": "Python version",
    "cli_version": "CLI version",
    "config_files": "Config files",
    "telemetry": "Telemetry",
    "index_cache": "Index cache",
    "vendor_root_codex": "Codex root",
    "vendor_root_claude": "Claude root",
    "vendor_root_pi": "Pi root",
}


@dataclass(frozen=True)
class TimeWindow:
    raw: str
    delta: timedelta | None

    @property
    def days(self) -> int | None:
        if self.delta is None:
            return None
        return int(self.delta.total_seconds() // 86_400)

    @property
    def unbounded(self) -> bool:
        return self.delta is None

    def cutoff(self, now: datetime) -> datetime | None:
        if self.delta is None:
            return None
        return now - self.delta


class IndexCacheRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_to_session_graph: dict[str, str]
    session_to_session_graph: dict[str, str]


class InvocationLogError(ValueError):
    def __init__(self, path: Path, line_number: int, detail: str) -> None:
        self.path = path
        self.line_number = line_number
        self.detail = detail
        super().__init__(f"{path}: line {line_number}: {detail}")


def _parse_duration(value: str) -> TimeWindow:
    """Parse doctor time windows."""
    if value == "all":
        return TimeWindow(raw=value, delta=None)
    if value in {"7d", "30d", "90d"}:
        return TimeWindow(raw=value, delta=timedelta(days=int(value[:-1])))
    raise argparse.ArgumentTypeError(
        f"unsupported --since value {value!r}; expected one of: {', '.join(_SUPPORTED_SINCE_VALUES)}"
    )


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(parts) or "invalid record"


def _read_invocation_log(path: Path, since: TimeWindow) -> list[InvocationRecord]:
    """Read and filter invocation log records."""
    if not path.exists():
        return []

    cutoff = since.cutoff(datetime.now(timezone.utc))
    records: list[InvocationRecord] = []

    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = InvocationRecord.model_validate_json(line)
            except ValidationError as exc:
                raise InvocationLogError(path, line_number, _format_validation_error(exc)) from exc
            if cutoff is None or record.ts >= cutoff:
                records.append(record)

    return records


def _make_check(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def _check_python_version() -> dict[str, Any]:
    """Check Python version."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 12):
        return _make_check("ok", version_str, version=version_str)
    return _make_check("fail", f"{version_str} (requires >= 3.12)", version=version_str)


def _get_cli_version() -> dict[str, Any]:
    """Get CLI version."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return _make_check("ok", version("coding-trajectory"))
    except PackageNotFoundError:
        return _make_check("warn", "unknown")
    except Exception as exc:
        return _make_check("warn", f"unknown ({exc})")


def _check_config_files() -> dict[str, Any]:
    """Check for config files."""
    config_toml = _CACHE_DIR / "config.toml"
    index_json = _CACHE_DIR / "index.json"

    present: list[str] = []
    if config_toml.exists():
        present.append("config.toml")
    if index_json.exists():
        present.append("index.json")

    if not present:
        return _make_check("warn", "no config files found", present=present)
    return _make_check("ok", ", ".join(present), present=present)


def _check_telemetry() -> dict[str, Any]:
    """Check telemetry settings."""
    decision = resolve_telemetry_decision()
    if decision.config_issue is not None:
        issue = decision.config_issue
        return _make_check(
            "fail",
            f"{decision.detail}; invalid config.toml: {issue.message}",
            source=decision.source,
            config_issue=issue.model_dump(mode="json"),
        )
    status = "ok" if decision.enabled else "warn"
    return _make_check(status, decision.detail, source=decision.source)


def _load_index_cache(path: Path) -> IndexCacheRecord:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read {path.name}: {exc}") from exc

    try:
        return IndexCacheRecord.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc


def _check_index_cache() -> tuple[dict[str, Any], IndexCacheRecord | None]:
    """Check index cache."""
    if not _INDEX_CACHE_PATH.exists():
        return _make_check("warn", "not found"), None

    try:
        stat = _INDEX_CACHE_PATH.stat()
    except OSError as exc:
        return _make_check("fail", f"unable to stat: {exc}"), None

    size_bytes = stat.st_size
    size_mb = size_bytes / (1024 * 1024)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    age_days = (datetime.now(timezone.utc) - mtime).days

    try:
        cache = _load_index_cache(_INDEX_CACHE_PATH)
    except ValueError as exc:
        return _make_check("fail", f"invalid schema: {exc}"), None

    path_mapping_count = len(cache.path_to_session_graph)
    session_mapping_count = len(cache.session_to_session_graph)
    detail = (
        f"{path_mapping_count} path mappings, {session_mapping_count} session mappings, "
        f"{size_mb:.1f}MB, {age_days}d old"
    )
    return (
        _make_check(
            "ok",
            detail,
            path_mapping_count=path_mapping_count,
            session_mapping_count=session_mapping_count,
            size_bytes=size_bytes,
            age_days=age_days,
        ),
        cache,
    )


def _check_vendor_root(path: Path) -> dict[str, Any]:
    """Check a single vendor root."""
    if not path.exists():
        return _make_check("warn", f"missing: {path}", path=str(path), exists=False)
    if not path.is_dir():
        return _make_check(
            "fail",
            f"not a directory: {path}",
            path=str(path),
            exists=True,
            is_directory=False,
        )
    if not os.access(path, os.R_OK | os.X_OK):
        return _make_check(
            "fail",
            f"unreadable: {path}",
            path=str(path),
            exists=True,
            is_directory=True,
            readable=False,
        )
    return _make_check(
        "ok",
        str(path),
        path=str(path),
        exists=True,
        is_directory=True,
        readable=True,
    )


def _vendor_root_checks() -> dict[str, dict[str, Any]]:
    home = Path.home()
    return {
        "vendor_root_codex": _check_vendor_root(home / ".codex" / "sessions"),
        "vendor_root_claude": _check_vendor_root(home / ".claude" / "projects"),
        "vendor_root_pi": _check_vendor_root(home / ".pi" / "agent" / "sessions"),
    }


def _compute_percentiles(values: list[float], percentiles: list[int]) -> dict[int, float]:
    """Compute percentiles with linear interpolation across sorted samples."""
    if not values:
        return {percentile: 0.0 for percentile in percentiles}

    ordered = sorted(values)
    if len(ordered) == 1:
        return {percentile: ordered[0] for percentile in percentiles}

    result: dict[int, float] = {}
    for percentile in percentiles:
        rank = (percentile / 100) * (len(ordered) - 1)
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            result[percentile] = ordered[lower]
            continue
        fraction = rank - lower
        result[percentile] = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return result


def _sorted_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _count_rows(counts: dict[str, int], key_name: str) -> list[dict[str, Any]]:
    return [{key_name: key, "count": count} for key, count in _sorted_counts(counts)]


def _aggregate_invocations(records: list[InvocationRecord]) -> dict[str, Any]:
    """Aggregate invocation statistics."""
    total = len(records)
    failed = sum(1 for record in records if not record.ok)

    latencies = [record.ms for record in records]
    percentiles = _compute_percentiles(latencies, [50, 95])

    cmd_counts: dict[str, int] = defaultdict(int)
    cwd_counts: dict[str, int] = defaultdict(int)
    vendor_counts: dict[str, int] = defaultdict(int)

    for record in records:
        cmd_counts[record.cmd] += 1
        cwd_counts[record.cwd] += 1
        if record.vendor:
            vendor_counts[record.vendor] += 1

    return {
        "total": total,
        "failed": failed,
        "latency_samples": len(latencies),
        "p50_ms": percentiles[50],
        "p95_ms": percentiles[95],
        "top_commands": _count_rows(dict(cmd_counts), "command")[:5],
        "top_cwds": _count_rows(dict(cwd_counts), "cwd")[:5],
        "vendors": _count_rows(dict(vendor_counts), "vendor"),
    }


def _aggregate_failures(records: list[InvocationRecord]) -> list[dict[str, Any]]:
    """Aggregate failure information."""
    groups: dict[tuple[str, str], list[InvocationRecord]] = defaultdict(list)
    for record in records:
        if record.ok:
            continue
        groups[(record.error or "unknown", record.cmd)].append(record)

    result: list[dict[str, Any]] = []
    for (error, cmd), group_records in groups.items():
        most_recent = max(group_records, key=lambda record: record.ts)
        result.append(
            {
                "error": error,
                "cmd": cmd,
                "count": len(group_records),
                "most_recent": {
                    "ts": most_recent.ts.isoformat(),
                    "cwd": most_recent.cwd,
                    "session_id": most_recent.session_id,
                },
                "sample": most_recent.model_dump(mode="json"),
            }
        )

    return sorted(result, key=lambda item: (-item["count"], item["error"], item["cmd"]))


def _aggregate_warnings(records: list[InvocationRecord]) -> list[dict[str, Any]]:
    """Aggregate warning information."""
    groups: dict[str, list[tuple[InvocationRecord, InvocationWarning]]] = defaultdict(list)

    for record in records:
        for warning in record.warnings:
            code = warning.code or "other"
            groups[code].append((record, warning))

    result: list[dict[str, Any]] = []
    for code, grouped in groups.items():
        severity_dist: dict[str, int] = defaultdict(int)
        cmd_counts: dict[str, int] = defaultdict(int)
        most_recent_record, most_recent_warning = max(grouped, key=lambda pair: pair[0].ts)

        for record, warning in grouped:
            severity_dist[warning.severity] += 1
            cmd_counts[record.cmd] += 1

        result.append(
            {
                "code": code,
                "count": len(grouped),
                "severity_dist": dict(sorted(severity_dist.items())),
                "top_commands": _count_rows(dict(cmd_counts), "command")[:3],
                "representative_message": most_recent_warning.message,
                "sample_context": most_recent_warning.context,
                "most_recent_ts": most_recent_record.ts.isoformat(),
            }
        )

    return sorted(result, key=lambda item: (-item["count"], item["code"]))


def _latency_bucket_label(timestamp: datetime, since: TimeWindow) -> tuple[str, str]:
    if since.unbounded or (since.days is not None and since.days > 30):
        iso_year, iso_week, _ = timestamp.isocalendar()
        return "weekly", f"{iso_year}-W{iso_week:02d}"
    return "daily", timestamp.strftime("%Y-%m-%d")


def _aggregate_latency_trends(records: list[InvocationRecord], since: TimeWindow) -> dict[str, Any]:
    """Aggregate latency trends by time bucket."""
    if not records:
        return {"granularity": "daily", "entries": []}

    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    granularity = "daily"

    for record in records:
        granularity, bucket = _latency_bucket_label(record.ts, since)
        buckets[bucket][record.cmd].append(record.ms)

    entries: list[dict[str, Any]] = []
    for bucket in sorted(buckets.keys()):
        commands = buckets[bucket]
        for cmd in sorted(commands.keys()):
            latencies = commands[cmd]
            percentiles = _compute_percentiles(latencies, [50, 95])
            entries.append(
                {
                    "bucket": bucket,
                    "command": cmd,
                    "count": len(latencies),
                    "p50_ms": percentiles[50],
                    "p95_ms": percentiles[95],
                }
            )

    return {"granularity": granularity, "entries": entries}


def _check_stale_state(cache: IndexCacheRecord | None) -> dict[str, Any]:
    """Check for stale index entries."""
    if cache is None:
        return {
            "path_mappings_scanned": 0,
            "stale_path_mappings": 0,
            "affected_session_graphs": 0,
            "entries": [],
        }

    entries = [
        {"path": path_str, "root_session_id": root_session_id}
        for path_str, root_session_id in cache.path_to_session_graph.items()
        if not Path(path_str).exists()
    ]
    affected_session_graphs = len({entry["root_session_id"] for entry in entries})
    return {
        "path_mappings_scanned": len(cache.path_to_session_graph),
        "stale_path_mappings": len(entries),
        "affected_session_graphs": affected_session_graphs,
        "entries": sorted(entries, key=lambda entry: (entry["root_session_id"], entry["path"])),
    }


def _format_ms(value: float) -> str:
    rounded = round(value, 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _render_markdown(
    env_checks: dict[str, dict[str, Any]],
    inv_summary: dict[str, Any],
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    latency_trends: dict[str, Any],
    stale: dict[str, Any],
    since: TimeWindow,
) -> str:
    """Render doctor report as markdown."""
    lines = ["# Doctor Report", "", f"Time window: {since.raw}", ""]

    lines.append("## Environment")
    lines.append("")
    for check_name in _ENVIRONMENT_CHECK_ORDER:
        check = env_checks[check_name]
        lines.append(
            f"- **{_ENVIRONMENT_CHECK_LABELS[check_name]}**: {check['status']} - {check['detail']}"
        )
    lines.append("")

    lines.append("## Invocation Summary")
    lines.append("")
    lines.append(f"- Total: {inv_summary['total']}")
    lines.append(f"- Failed: {inv_summary['failed']}")
    lines.append(f"- Latency samples: {inv_summary['latency_samples']}")
    lines.append(f"- P50 latency: {_format_ms(inv_summary['p50_ms'])}ms")
    lines.append(f"- P95 latency: {_format_ms(inv_summary['p95_ms'])}ms")
    lines.append("")

    if inv_summary["top_commands"]:
        lines.append("Top commands:")
        for item in inv_summary["top_commands"]:
            lines.append(f"  - {item['command']}: {item['count']}")
        lines.append("")

    if inv_summary["top_cwds"]:
        lines.append("Top working directories:")
        for item in inv_summary["top_cwds"]:
            lines.append(f"  - {item['cwd']}: {item['count']}")
        lines.append("")

    if inv_summary["vendors"]:
        lines.append("Vendors:")
        for item in inv_summary["vendors"]:
            lines.append(f"  - {item['vendor']}: {item['count']}")
        lines.append("")

    if failures:
        lines.append("## Failures")
        lines.append("")
        visible_failures = failures[:_MARKDOWN_FAILURE_LIMIT]
        if len(failures) > len(visible_failures):
            lines.append(f"Showing {len(visible_failures)} of {len(failures)} failure groups.")
            lines.append("")
        for failure in visible_failures:
            lines.append(f"### {failure['error']} in `{failure['cmd']}`")
            lines.append(f"- Count: {failure['count']}")
            recent = failure["most_recent"]
            lines.append(f"- Most recent: {recent['ts']}")
            if recent.get("cwd"):
                lines.append(f"  - cwd: {recent['cwd']}")
            if recent.get("session_id"):
                lines.append(f"  - session: {recent['session_id']}")
            lines.append("")

    if warnings:
        lines.append("## Warnings")
        lines.append("")
        visible_warnings = warnings[:_MARKDOWN_WARNING_LIMIT]
        if len(warnings) > len(visible_warnings):
            lines.append(f"Showing {len(visible_warnings)} of {len(warnings)} warning groups.")
            lines.append("")
        for warning in visible_warnings:
            lines.append(f"### {warning['code']}")
            lines.append(f"- Count: {warning['count']}")
            if warning["severity_dist"]:
                severity_parts = [
                    f"{severity}: {count}" for severity, count in warning["severity_dist"].items()
                ]
                lines.append(f"- Severity: {', '.join(severity_parts)}")
            if warning["top_commands"]:
                commands = ", ".join(
                    f"{item['command']} ({item['count']})" for item in warning["top_commands"]
                )
                lines.append(f"- Top commands: {commands}")
            if warning.get("representative_message"):
                lines.append(f"- Message: {warning['representative_message']}")
            lines.append("")

    trend_entries = latency_trends["entries"]
    if trend_entries:
        lines.append("## Latency Trends")
        lines.append("")
        visible_entries = trend_entries[-_MARKDOWN_TREND_LIMIT:]
        if len(trend_entries) > len(visible_entries):
            lines.append(
                f"Showing latest {len(visible_entries)} of {len(trend_entries)} "
                f"{latency_trends['granularity']} bucket rows."
            )
            lines.append("")
        lines.append("```")
        lines.append(f"{'Bucket':<12} {'Command':<24} {'Count':>6} {'P50':>8} {'P95':>8}")
        for entry in visible_entries:
            lines.append(
                f"{entry['bucket']:<12} {entry['command']:<24} {entry['count']:>6} "
                f"{_format_ms(entry['p50_ms']):>8} {_format_ms(entry['p95_ms']):>8}"
            )
        lines.append("```")
        lines.append("")

    if stale["entries"]:
        lines.append("## Stale State")
        lines.append("")
        lines.append(
            f"Found {stale['stale_path_mappings']} stale path mappings across "
            f"{stale['affected_session_graphs']} session graphs."
        )
        visible_entries = stale["entries"][:_MARKDOWN_STALE_LIMIT]
        if len(stale["entries"]) > len(visible_entries):
            lines.append(f"Showing {len(visible_entries)} of {len(stale['entries'])} stale entries.")
        for entry in visible_entries:
            lines.append(f"- [{entry['root_session_id']}] {entry['path']}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _render_json(
    env_checks: dict[str, dict[str, Any]],
    inv_summary: dict[str, Any],
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    latency_trends: dict[str, Any],
    stale: dict[str, Any],
    since: TimeWindow,
) -> dict[str, Any]:
    """Render doctor report as JSON."""
    return {
        "doctor_version": 2,
        "time_window": {
            "value": since.raw,
            "days": since.days,
            "unbounded": since.unbounded,
        },
        "time_window_days": since.days,
        "environment": env_checks,
        "invocation_summary": inv_summary,
        "failures": failures,
        "warnings": warnings,
        "latency_trends": latency_trends["entries"],
        "latency_trend_granularity": latency_trends["granularity"],
        "stale_state": stale["entries"],
        "stale_state_summary": {
            "path_mappings_scanned": stale["path_mappings_scanned"],
            "stale_path_mappings": stale["stale_path_mappings"],
            "affected_session_graphs": stale["affected_session_graphs"],
        },
    }


def _doctor_handler(args: argparse.Namespace) -> CommandOutcome:
    """Main handler for the doctor command."""
    since = args.since if isinstance(args.since, TimeWindow) else _parse_duration(str(args.since))

    try:
        records = _read_invocation_log(invocation_log_path(), since)
    except InvocationLogError as exc:
        message = f"Error reading invocation log: {exc}"
        print(message, file=sys.stderr)
        return CommandOutcome.failed(exit_code=3, error=message)
    except (OSError, UnicodeError) as exc:
        message = f"Error reading invocation log: {exc}"
        print(message, file=sys.stderr)
        return CommandOutcome.failed(exit_code=3, error=message)

    index_cache_check, index_cache = _check_index_cache()
    env_checks = {
        "python_version": _check_python_version(),
        "cli_version": _get_cli_version(),
        "config_files": _check_config_files(),
        "telemetry": _check_telemetry(),
        "index_cache": index_cache_check,
        **_vendor_root_checks(),
    }

    inv_summary = _aggregate_invocations(records)
    failures = _aggregate_failures(records)
    warnings = _aggregate_warnings(records)
    latency_trends = _aggregate_latency_trends(records, since)
    stale = _check_stale_state(index_cache)

    if args.output_format == "json":
        report = _render_json(env_checks, inv_summary, failures, warnings, latency_trends, stale, since)
        print(json.dumps(report, indent=2))
    else:
        report = _render_markdown(
            env_checks, inv_summary, failures, warnings, latency_trends, stale, since
        )
        print(report)

    has_fail_status = any(env_checks[name]["status"] == "fail" for name in env_checks)
    has_warn_status = any(env_checks[name]["status"] == "warn" for name in env_checks)
    has_failed_invocations = inv_summary["failed"] > 0

    if has_fail_status:
        return CommandOutcome.completed(exit_code=2)
    if has_warn_status or has_failed_invocations:
        return CommandOutcome.completed(exit_code=1)
    return CommandOutcome.completed(exit_code=0)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the doctor command."""
    doctor_parser = subparsers.add_parser(
        "doctor",
        prog="ct doctor",
        help="Run diagnostic checks and show invocation telemetry.",
        formatter_class=GhFormatter,
    )
    doctor_parser.add_argument(
        "--since",
        type=_parse_duration,
        default=_parse_duration("7d"),
        metavar="DURATION",
        help="Time window: 7d, 30d, 90d, or all (default: 7d)",
    )
    doctor_parser.add_argument(
        "--output",
        "--format",
        "-o",
        dest="output_format",
        choices=["markdown", "json"],
        default="markdown",
        metavar="{markdown,json}",
        help="Output format (default: markdown)",
    )
    doctor_parser.set_defaults(_plugin_handler=_doctor_handler)
