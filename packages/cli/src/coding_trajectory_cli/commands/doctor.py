"""Doctor command: diagnostic surface for environment and invocation telemetry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from coding_trajectory_cli._shared import GhFormatter


def _parse_duration(value: str) -> timedelta:
    """Parse duration strings like '7d', '30d', '90d'."""
    if value == "all":
        return timedelta(days=365 * 10)  # 10 years as "all"
    if value.endswith("d"):
        return timedelta(days=int(value[:-1]))
    raise ValueError(f"Invalid duration: {value}. Use '7d', '30d', '90d', or 'all'.")


def _read_invocation_log(path: Path, since: timedelta) -> list[dict[str, Any]]:
    """Read and filter invocation log records."""
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - since
    records = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ts_str = record.get("ts")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        records.append(record)
            except (json.JSONDecodeError, ValueError):
                continue

    return records


def _check_python_version() -> tuple[str, str]:
    """Check Python version."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 12):
        return "ok", version_str
    return "fail", f"{version_str} (requires >= 3.12)"


def _get_cli_version() -> tuple[str, str]:
    """Get CLI version."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return "ok", version("coding-trajectory")
    except Exception:
        return "warn", "unknown"


def _check_config_files() -> tuple[str, str]:
    """Check for config files."""
    config_dir = Path.home() / ".coding-trajectory"
    config_toml = config_dir / "config.toml"
    index_json = config_dir / "index.json"

    present = []
    if config_toml.exists():
        present.append("config.toml")
    if index_json.exists():
        present.append("index.json")

    if not present:
        return "warn", "no config files found"
    return "ok", ", ".join(present)


def _check_telemetry() -> tuple[str, str]:
    """Check telemetry settings."""
    env_val = os.environ.get("CT_TELEMETRY", "").strip().lower()
    if env_val in {"0", "false", "no", "off"}:
        return "warn", "disabled via CT_TELEMETRY"

    config_toml = Path.home() / ".coding-trajectory" / "config.toml"
    if config_toml.exists():
        try:
            with open(config_toml, "rb") as f:
                config = tomllib.load(f)
            telemetry_config = config.get("telemetry", {})
            if isinstance(telemetry_config, dict):
                enabled = telemetry_config.get("enabled", True)
                if not enabled:
                    return "warn", "disabled in config.toml"
        except Exception:
            pass

    return "ok", "enabled"


def _check_index_cache() -> tuple[str, str]:
    """Check index cache."""
    index_json = Path.home() / ".coding-trajectory" / "index.json"
    if not index_json.exists():
        return "warn", "not found"

    try:
        stat = index_json.stat()
        size_mb = stat.st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - mtime).days

        with open(index_json, "r", encoding="utf-8") as f:
            data = json.load(f)
            entry_count = len(data) if isinstance(data, dict) else 0

        return "ok", f"{entry_count} entries, {size_mb:.1f}MB, {age_days}d old"
    except Exception as e:
        return "warn", f"error reading: {e}"


def _check_vendor_paths() -> tuple[str, str]:
    """Check vendor log paths."""
    home = Path.home()
    paths = {
        "codex": home / ".codex" / "sessions",
        "claude": home / ".claude" / "projects",
        "pi": home / ".pi" / "agent" / "sessions",
    }

    reachable = []
    for vendor, path in paths.items():
        if path.exists():
            reachable.append(vendor)

    if not reachable:
        return "warn", "no vendor paths found"
    return "ok", ", ".join(reachable)


def _compute_percentiles(values: list[int], percentiles: list[int]) -> dict[int, int]:
    """Compute percentiles from a list of values."""
    if not values:
        return {p: 0 for p in percentiles}

    sorted_vals = sorted(values)
    result = {}
    for p in percentiles:
        idx = int(len(sorted_vals) * p / 100)
        idx = min(idx, len(sorted_vals) - 1)
        result[p] = sorted_vals[idx]
    return result


def _aggregate_invocations(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate invocation statistics."""
    total = len(records)
    failed = sum(1 for r in records if not r.get("ok", True))

    latencies = [r.get("ms", 0) for r in records if "ms" in r]
    percentiles = _compute_percentiles(latencies, [50, 95])

    cmd_counts = defaultdict(int)
    cwd_counts = defaultdict(int)
    vendor_counts = defaultdict(int)

    for r in records:
        cmd = r.get("cmd")
        if cmd:
            cmd_counts[cmd] += 1
        cwd = r.get("cwd")
        if cwd:
            cwd_counts[cwd] += 1
        vendor = r.get("vendor")
        if vendor:
            vendor_counts[vendor] += 1

    top_commands = sorted(cmd_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_cwds = sorted(cwd_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    vendors = sorted(vendor_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total": total,
        "failed": failed,
        "p50_ms": percentiles[50],
        "p95_ms": percentiles[95],
        "top_commands": top_commands,
        "top_cwds": top_cwds,
        "vendors": vendors,
    }


def _aggregate_failures(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate failure information."""
    failures = [r for r in records if not r.get("ok", True)]

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in failures:
        error = r.get("error") or "unknown"
        cmd = r.get("cmd") or "unknown"
        groups[(error, cmd)].append(r)

    result = []
    for (error, cmd), group_records in groups.items():
        most_recent = max(group_records, key=lambda r: r.get("ts", ""))
        result.append({
            "error": error,
            "cmd": cmd,
            "count": len(group_records),
            "most_recent": {
                "ts": most_recent.get("ts"),
                "cwd": most_recent.get("cwd"),
                "session_id": most_recent.get("session_id"),
            },
            "sample": most_recent,
        })

    return sorted(result, key=lambda x: x["count"], reverse=True)


def _aggregate_warnings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate warning information."""
    all_warnings: list[tuple[str, dict[str, Any]]] = []

    for r in records:
        warnings = r.get("warnings") or []
        cmd = r.get("cmd") or "unknown"
        for w in warnings:
            if isinstance(w, dict):
                code = w.get("code") or "other"
                all_warnings.append((code, {**w, "_cmd": cmd}))

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for code, warning in all_warnings:
        groups[code].append(warning)

    result = []
    for code, warnings in groups.items():
        severity_dist = defaultdict(int)
        cmd_counts = defaultdict(int)
        for w in warnings:
            severity = w.get("severity", "warning")
            severity_dist[severity] += 1
            cmd_counts[w.get("_cmd", "unknown")] += 1

        top_cmds = sorted(cmd_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        representative = warnings[0]

        result.append({
            "code": code,
            "count": len(warnings),
            "severity_dist": dict(severity_dist),
            "top_commands": top_cmds,
            "representative_message": representative.get("message"),
        })

    result = sorted(result, key=lambda x: x["count"], reverse=True)

    # Group single-occurrence codes under "Other"
    if len(result) > 10:
        main = [r for r in result if r["count"] > 1]
        other_count = sum(r["count"] for r in result if r["count"] == 1)
        if other_count > 0:
            main.append({
                "code": "other",
                "count": other_count,
                "severity_dist": {},
                "top_commands": [],
                "representative_message": f"{other_count} single-occurrence warnings",
            })
        return main

    return result


def _aggregate_latency_trends(records: list[dict[str, Any]], since: timedelta) -> list[dict[str, Any]]:
    """Aggregate latency trends by time bucket."""
    if not records:
        return []

    # Determine bucket size based on time window
    if since.days > 30:
        bucket_fmt = lambda dt: dt.strftime("%Y-W%U")  # Weekly
    else:
        bucket_fmt = lambda dt: dt.strftime("%Y-%m-%d")  # Daily

    # Group by bucket and command
    buckets: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))

    for r in records:
        ts_str = r.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bucket = bucket_fmt(ts)
            cmd = r.get("cmd") or "unknown"
            ms = r.get("ms")
            if ms is not None:
                buckets[bucket][cmd].append(ms)
        except (ValueError, TypeError):
            continue

    # Compute percentiles per bucket
    result = []
    for bucket in sorted(buckets.keys()):
        cmds = buckets[bucket]
        for cmd, latencies in sorted(cmds.items()):
            percentiles = _compute_percentiles(latencies, [50, 95])
            result.append({
                "bucket": bucket,
                "command": cmd,
                "count": len(latencies),
                "p50_ms": percentiles[50],
                "p95_ms": percentiles[95],
            })

    return result


def _check_stale_state() -> list[dict[str, Any]]:
    """Check for stale index entries."""
    index_json = Path.home() / ".coding-trajectory" / "index.json"
    if not index_json.exists():
        return []

    try:
        with open(index_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return []

        stale = []
        for key, value in data.items():
            # Check if this is a path-based entry
            if isinstance(value, dict):
                path_str = value.get("path") or key
                if path_str and not path_str.startswith("http"):
                    path = Path(path_str)
                    if not path.exists():
                        stale.append({
                            "key": key,
                            "path": path_str,
                        })

        return stale[:20]  # Limit to 20 examples
    except Exception:
        return []


def _render_markdown(
    env_checks: dict[str, tuple[str, str]],
    inv_summary: dict[str, Any],
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    latency_trends: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    since: timedelta,
) -> str:
    """Render doctor report as markdown."""
    lines = ["# Doctor Report", "", f"Time window: {since.days} days", ""]

    # Environment section
    lines.append("## Environment")
    lines.append("")
    for check_name, (status, detail) in env_checks.items():
        status_icon = {"ok": "ok", "warn": "warn", "fail": "fail"}[status]
        lines.append(f"- **{check_name}**: {status_icon} — {detail}")
    lines.append("")

    # Invocation summary
    lines.append("## Invocation Summary")
    lines.append("")
    lines.append(f"- Total: {inv_summary['total']}")
    lines.append(f"- Failed: {inv_summary['failed']}")
    lines.append(f"- P50 latency: {inv_summary['p50_ms']}ms")
    lines.append(f"- P95 latency: {inv_summary['p95_ms']}ms")
    lines.append("")

    if inv_summary["top_commands"]:
        lines.append("Top commands:")
        for cmd, count in inv_summary["top_commands"]:
            lines.append(f"  - {cmd}: {count}")
        lines.append("")

    if inv_summary["top_cwds"]:
        lines.append("Top working directories:")
        for cwd, count in inv_summary["top_cwds"]:
            lines.append(f"  - {cwd}: {count}")
        lines.append("")

    if inv_summary["vendors"]:
        lines.append("Vendors:")
        for vendor, count in inv_summary["vendors"]:
            lines.append(f"  - {vendor}: {count}")
        lines.append("")

    # Failure summary
    if failures:
        lines.append("## Failures")
        lines.append("")
        for fail in failures[:10]:  # Limit to 10
            lines.append(f"### {fail['error']} in `{fail['cmd']}`")
            lines.append(f"- Count: {fail['count']}")
            recent = fail["most_recent"]
            lines.append(f"- Most recent: {recent['ts']}")
            if recent.get("cwd"):
                lines.append(f"  - cwd: {recent['cwd']}")
            if recent.get("session_id"):
                lines.append(f"  - session: {recent['session_id']}")
            lines.append("")

    # Warning summary
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warn in warnings[:10]:  # Limit to 10
            lines.append(f"### {warn['code']}")
            lines.append(f"- Count: {warn['count']}")
            if warn["severity_dist"]:
                sev_parts = [f"{k}: {v}" for k, v in warn["severity_dist"].items()]
                lines.append(f"- Severity: {', '.join(sev_parts)}")
            if warn["top_commands"]:
                cmds = ", ".join(f"{c} ({n})" for c, n in warn["top_commands"])
                lines.append(f"- Top commands: {cmds}")
            if warn.get("representative_message"):
                lines.append(f"- Message: {warn['representative_message']}")
            lines.append("")

    # Latency trends
    if latency_trends:
        lines.append("## Latency Trends")
        lines.append("")
        lines.append("```")
        lines.append(f"{'Bucket':<12} {'Command':<24} {'Count':>6} {'P50':>6} {'P95':>6}")
        for trend in latency_trends[-20:]:  # Last 20 entries
            lines.append(
                f"{trend['bucket']:<12} {trend['command']:<24} {trend['count']:>6} "
                f"{trend['p50_ms']:>6} {trend['p95_ms']:>6}"
            )
        lines.append("```")
        lines.append("")

    # Stale state
    if stale:
        lines.append("## Stale State")
        lines.append("")
        lines.append(f"Found {len(stale)} stale index entries:")
        for entry in stale[:10]:  # Limit to 10
            lines.append(f"- {entry['key']}: {entry['path']}")
        if len(stale) > 10:
            lines.append(f"- ... and {len(stale) - 10} more")
        lines.append("")

    return "\n".join(lines).rstrip()


def _render_json(
    env_checks: dict[str, tuple[str, str]],
    inv_summary: dict[str, Any],
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    latency_trends: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    since: timedelta,
) -> dict[str, Any]:
    """Render doctor report as JSON."""
    return {
        "doctor_version": 1,
        "time_window_days": since.days,
        "environment": {
            name: {"status": status, "detail": detail}
            for name, (status, detail) in env_checks.items()
        },
        "invocation_summary": inv_summary,
        "failures": failures,
        "warnings": warnings,
        "latency_trends": latency_trends,
        "stale_state": stale,
    }


def _doctor_handler(args: argparse.Namespace) -> int:
    """Main handler for the doctor command."""
    since = _parse_duration(args.since)

    # Read invocation log
    log_path = Path.home() / ".coding-trajectory" / "invocations.jsonl"
    try:
        records = _read_invocation_log(log_path, since)
    except Exception as e:
        print(f"Error reading invocation log: {e}", file=sys.stderr)
        return 3

    # Environment checks
    env_checks = {
        "Python version": _check_python_version(),
        "CLI version": _get_cli_version(),
        "Config files": _check_config_files(),
        "Telemetry": _check_telemetry(),
        "Index cache": _check_index_cache(),
        "Vendor paths": _check_vendor_paths(),
    }

    # Aggregations
    inv_summary = _aggregate_invocations(records)
    failures = _aggregate_failures(records)
    warnings = _aggregate_warnings(records)
    latency_trends = _aggregate_latency_trends(records, since)
    stale = _check_stale_state()

    # Render output
    if args.output_format == "json":
        report = _render_json(env_checks, inv_summary, failures, warnings, latency_trends, stale, since)
        print(json.dumps(report, indent=2))
    else:
        report = _render_markdown(env_checks, inv_summary, failures, warnings, latency_trends, stale, since)
        print(report)

    # Determine exit code
    has_fail_status = any(status == "fail" for _, (status, _) in env_checks.items())
    has_warn_status = any(status == "warn" for _, (status, _) in env_checks.items())
    has_failed_invocations = inv_summary["failed"] > 0

    if has_fail_status:
        return 2
    if has_warn_status or has_failed_invocations:
        return 1
    return 0


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
        default="7d",
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
