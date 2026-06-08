from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ct plugin activity",
        description="Inspect recent activity across sessions.",
    )
    parser.add_argument(
        "--window",
        choices=("5h", "today", "72h", "7d"),
        default="today",
        help="Time window to inspect. Defaults to today.",
    )
    parser.add_argument("--project", default=None, help="Filter to one project name.")
    parser.add_argument("--account", default=None, help="Accepted for compatibility; currently shown as a filter only.")
    parser.add_argument("--agent-vendor", default=None, help="Filter by agent vendor.")
    args = parser.parse_args(argv)

    since_days = {"5h": 1, "today": 1, "72h": 3, "7d": 7}[args.window]
    params: dict[str, Any] = {"since_days": since_days}
    if args.project:
        params["project_name"] = args.project
    if args.agent_vendor:
        params["agent_vendor"] = args.agent_vendor

    payload = _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])
    sessions = payload.get("items") or []
    projects = sorted({item.get("title") or item.get("project") or args.project or "unknown" for item in sessions})
    vendors = sorted({vendor for item in sessions for vendor in item.get("vendors") or []})
    result = {
        "command": "activity",
        "window": args.window,
        "filters": {
            "project": args.project,
            "account": args.account,
            "agent_vendor": args.agent_vendor,
        },
        "totals": {
            "session_count": len(sessions),
            "project_count": len(projects),
            "vendor_count": len(vendors),
        },
        "vendors": vendors,
        "sessions": sessions,
    }
    print(f"Activity ({args.window})")
    print(f"Sessions {len(sessions)}  Projects {len(projects)}  Vendors {len(vendors)}")
    if args.account:
        print("Note: account filtering requires account fields in documented ct project/session outputs.")
    print("")
    if not sessions:
        print("No matching sessions.")
        return 0
    for item in sessions[:10]:
        root_id = str(item.get("root_session_id") or "-")
        title = _one_line(item.get("title") or "-", 96)
        vendors_text = ",".join(item.get("vendors") or []) or "-"
        print(f"{root_id[:8]}  {vendors_text:<18} {title}")
    if len(sessions) > 10:
        print(f"... {len(sessions) - 10} more")
    return 0


def _ct_json(args: list[str]) -> dict[str, Any]:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        raise SystemExit("ct executable not found; set CT_COMMAND to the ct command path")
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ct command timed out: {' '.join(command)}") from exc
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        raise SystemExit(completed.returncode)
    return json.loads(completed.stdout)


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
