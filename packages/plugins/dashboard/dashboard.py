from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Any

import cleanup as cleanup_mod


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_root_parser()
    if not raw_args:
        print(_root_entry_text())
        return 0
    if raw_args[0] in {"-h", "--help"}:
        print(parser.format_help())
        return 0
    if raw_args == ["--tui"]:
        return _run_dashboard_tui()
    if raw_args[0] in {"web", "--web"}:
        return _run_dashboard_web(raw_args[1:])
    action, rest = raw_args[0], raw_args[1:]
    if action == "project":
        return _handle_project_command(rest)
    if action == "session":
        return _handle_session_command(rest)
    parser.parse_args(raw_args)
    return 2


def _projects(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {}
    if args.agent_vendor:
        params["agent_vendor"] = args.agent_vendor
    payload = _ct_json(["project", "list", "--params", json.dumps(params), "--output", "json"])
    items = payload.get("items") or {}
    print(f"Projects ({len(items)})")
    for name, meta in sorted(items.items()):
        vendors = ", ".join(meta.get("vendors") or []) or "-"
        print(f"  {name:<28} {vendors:<20} {meta.get('path') or '-'}")
    return 0


def _sessions(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"since_days": None if args.all_time else args.since_days}
    if args.project_name:
        params["project_name"] = args.project_name
    if args.agent_vendor:
        params["agent_vendor"] = args.agent_vendor
    payload = _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])
    sessions = payload.get("items") or []
    print(f"Sessions ({len(sessions)})")
    for item in sessions[:20]:
        root_id = str(item.get("id") or "-")
        vendors = ", ".join(item.get("vendors") or []) or "-"
        title = _one_line(item.get("title") or "-", 88)
        print(f"  {root_id[:8]}  {vendors:<18} {title}")
    if len(sessions) > 20:
        print(f"  ... {len(sessions) - 20} more")
    return 0


def _build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard",
        description=_root_entry_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tui", action="store_true", help="Quick inspection and cleanup (terminal).")
    parser.add_argument("--web", action="store_true", help="Open the dashboard web program.")
    sub = parser.add_subparsers(dest="action", metavar="<command>")
    sub.add_parser("web", help="Rich dashboard with analytics (browser).")
    project = sub.add_parser("project", help="List managed projects and project actions.")
    project.add_argument("--agent-vendor", default=None)
    project_cleanup = sub.add_parser(
        "project cleanup",
        help="Clean old project directories.",
    )
    project_cleanup.add_argument("--older-than", default="30d")
    project_cleanup.add_argument("--path", default=None)
    project_cleanup.add_argument("--dry-run", action="store_true")
    project_cleanup.add_argument("--detail", action="store_true")
    session = sub.add_parser("session", help="List sessions and session actions.")
    session.add_argument("project_name", nargs="?")
    session.add_argument("--since-days", type=int, default=30)
    session.add_argument("--all-time", action="store_true")
    session.add_argument("--agent-vendor", default=None)
    session_cleanup = sub.add_parser(
        "session cleanup",
        help="Clean empty or low-value session logs.",
    )
    session_cleanup.add_argument("--agent-vendor", default=None)
    session_cleanup.add_argument("--trash", action="store_true")
    session_cleanup.add_argument("--delete", action="store_true")
    session_cleanup.add_argument("--confirm", action="store_true")
    session_cleanup.add_argument("--tui", action="store_true")
    return parser


def _project_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard project",
        description="Project management commands.",
        epilog="SUBCOMMANDS\n  cleanup   Clean old project directories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent-vendor", default=None)
    return parser


def _project_cleanup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard project cleanup",
        description="Permanently delete old project directories.",
    )
    parser.add_argument("--older-than", default="30d")
    parser.add_argument("--path", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching cleanup candidates without deleting them.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print the full cleanup payload as JSON.",
    )
    return parser


def _session_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard session",
        description="Session management commands.",
        epilog="SUBCOMMANDS\n  cleanup   Clean empty or low-value session logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project_name", nargs="?")
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--all-time", action="store_true")
    parser.add_argument("--agent-vendor", default=None)
    return parser


def _session_cleanup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard session cleanup",
        description="Clean empty or low-value session logs.",
    )
    parser.add_argument("--agent-vendor", default=None)
    parser.add_argument("--trash", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--tui", action="store_true")
    return parser


def _handle_project_command(args: list[str]) -> int:
    if args and args[0] == "cleanup":
        parsed = _project_cleanup_parser().parse_args(args[1:])
        return _project_cleanup(parsed)
    parsed = _project_list_parser().parse_args(args)
    return _projects(parsed)


def _handle_session_command(args: list[str]) -> int:
    if args and args[0] == "cleanup":
        parsed = _session_cleanup_parser().parse_args(args[1:])
        return _session_cleanup(parsed)
    parsed = _session_list_parser().parse_args(args)
    return _sessions(parsed)


def _root_entry_text() -> str:
    return "\n".join(
        [
            "Dashboard executable plugin",
            "",
            "Commands:",
            "  ct plugin dashboard --tui          Quick inspection and cleanup (terminal)",
            "  ct plugin dashboard web [flags]    Rich dashboard with analytics (browser)",
            "  ct plugin dashboard project [--agent-vendor VENDOR]",
            "  ct plugin dashboard project cleanup [--dry-run] [flags]",
            "  ct plugin dashboard session [PROJECT] [--since-days N|--all-time]",
            "  ct plugin dashboard session cleanup [--tui] [flags]",
            "",
            "TUI:  Simple interaction, browse projects/sessions, cleanup with keyboard.",
            "Web:  Rich UI with charts, filters, vendor breakdown, session timeline.",
        ]
    )


def _project_cleanup(args: argparse.Namespace) -> int:
    try:
        payload = cleanup_mod.handle_project(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(cleanup_mod.render(args, payload))
    return 1 if (payload.get("summary") or {}).get("error_count") else 0


def _session_cleanup(args: argparse.Namespace) -> int:
    try:
        payload = cleanup_mod.handle_session(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(cleanup_mod.render(args, payload))
    return 1 if (payload.get("summary") or {}).get("error_count") else 0


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


def _project_payload() -> dict[str, Any]:
    return _ct_json(["project", "list", "--params", json.dumps({}), "--output", "json"])


def _session_payload() -> dict[str, Any]:
    params = {"since_days": 30}
    return _ct_json(["project", "sessions", "--params", json.dumps(params), "--output", "json"])


def _run_dashboard_tui() -> int:
    try:
        from dashboard_tui import DashboardServices, run_dashboard_tui
    except ImportError:
        print(
            "error: --tui requires the optional 'textual' dependency in the plugin environment",
            file=sys.stderr,
        )
        return 2

    project_cleanup_args = argparse.Namespace(
        older_than="30d",
        path=None,
        trash=False,
        delete=False,
        confirm=False,
        tui=False,
        detail=False,
    )
    session_cleanup_args = argparse.Namespace(
        agent_vendor=None,
        trash=False,
        delete=False,
        confirm=False,
        tui=False,
        detail=False,
    )
    services = DashboardServices(
        load_projects=_project_payload,
        load_sessions=_session_payload,
        preview_project_cleanup=lambda: cleanup_mod.preview_project_cleanup(project_cleanup_args),
        preview_session_cleanup=lambda: cleanup_mod.preview_session_cleanup(session_cleanup_args),
        apply_project_cleanup=lambda preview, action, selected: cleanup_mod.apply_project_selection(
            argparse.Namespace(
                older_than="30d",
                path=None,
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
                tui=False,
                detail=False,
            ),
            preview,
            action,
            selected,
        ),
        apply_session_cleanup=lambda preview, action, selected: cleanup_mod.apply_session_selection(
            argparse.Namespace(
                agent_vendor=None,
                trash=action == "trash",
                delete=action == "delete",
                confirm=True,
                tui=False,
                detail=False,
            ),
            preview,
            action,
            selected,
        ),
    )
    run_dashboard_tui(services)
    return 0


def _run_dashboard_web(args: list[str]) -> int:
    try:
        from dashboard_web import main as web_main
    except ImportError as exc:
        print(f"error: dashboard web entrypoint could not be loaded: {exc}", file=sys.stderr)
        return 2
    return web_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
