from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys

try:
    from . import cleanup as cleanup_mod
    from . import context_window as context_window_mod
except ImportError:
    import cleanup as cleanup_mod
    import context_window as context_window_mod


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_root_parser()
    if not raw_args:
        print(_root_entry_text())
        return 0
    if raw_args[0] in {"web", "--web"}:
        return _run_dashboard_web(raw_args[1:])
    action, rest = raw_args[0], raw_args[1:]
    if action == "project":
        return _handle_project_command(rest)
    if action == "session":
        return _handle_session_command(rest)
    parser.parse_args(raw_args)
    return 2


def _build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard",
        description=_root_entry_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    session_context_window = sub.add_parser(
        "session context-window",
        help="Inspect session context composition and trajectory events.",
    )
    session_context_window.add_argument("session_id")
    session_context_window.add_argument("--turn", dest="turn_id", default=None)
    session_context_window.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
    )
    return parser


def _project_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard project",
        description="Alias for `ct project list`.",
        epilog="SUBCOMMANDS\n  cleanup   Clean old project directories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        description="Alias for `ct project sessions`.",
        epilog=(
            "SUBCOMMANDS\n"
            "  cleanup          Clean empty or low-value session logs.\n"
            "  context-window   Inspect context composition and trajectory events."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    return parser


def _handle_project_command(args: list[str]) -> int:
    if args and args[0] == "cleanup":
        parsed = _project_cleanup_parser().parse_args(args[1:])
        return _project_cleanup(parsed)
    return _ct_passthrough(["project", "list", *args])


def _handle_session_command(args: list[str]) -> int:
    if args and args[0] == "cleanup":
        parsed = _session_cleanup_parser().parse_args(args[1:])
        return _session_cleanup(parsed)
    if args and args[0] == "context-window":
        return context_window_mod.main(args[1:])
    return _ct_passthrough(["project", "sessions", *args])


def _root_entry_text() -> str:
    return "\n".join(
        [
            "Dashboard executable plugin",
            "",
            "Commands:",
            "  ct plugin dashboard web [flags]    Rich dashboard with analytics (browser)",
            "  ct plugin dashboard project [ct project list flags]",
            "  ct plugin dashboard project cleanup [--dry-run] [flags]",
            "  ct plugin dashboard session [ct project sessions flags]",
            "  ct plugin dashboard session cleanup [flags]",
            "  ct plugin dashboard session context-window SESSION_ID [flags]",
            "",
            "Web:  Overview-first UI with explicit routes for browsing and cleanup.",
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


def _ct_passthrough(args: list[str]) -> int:
    ct = os.environ.get("CT_COMMAND") or shutil.which("ct")
    if not ct:
        print("error: ct executable not found; set CT_COMMAND to the ct command path", file=sys.stderr)
        return 127
    command = [*shlex.split(ct), *args]
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


def _run_dashboard_web(args: list[str]) -> int:
    try:
        from .dashboard_web import main as web_main
    except ImportError as exc:
        try:
            from dashboard_web import main as web_main
        except ImportError:
            print(f"error: dashboard web entrypoint could not be loaded: {exc}", file=sys.stderr)
            return 2
    return web_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
