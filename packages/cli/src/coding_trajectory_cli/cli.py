"""Command-line interface for reading coding session graph data."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from coding_trajectory import debug
from coding_trajectory.contracts import service_contract
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.runtime import ServiceRuntime
from coding_trajectory_cli._shared import (
    GhFormatter,
    add_output_flags,
    compact_payload,
    json_text,
    render_markdown_for_terminal,
    selected_output,
)
from coding_trajectory_cli.commands import REGISTRARS, dispatch_plugin_argv

EPILOG = """\
NOTE
  Sessions are located automatically via cache; pass a SESSION_ID to use
  that coding session as the session tree entry point, or omit it to use the
  most-recent session in the current working directory.
"""

_TELEMETRY_DISABLED = {"0", "false", "no", "off"}


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    method: str = args._method
    params: dict[str, Any] = args._params(args)
    effective_global_scope = True if method == "project.list" else args.global_scope
    with ServiceRuntime(
        global_scope=effective_global_scope,
        current_dir=Path.cwd(),
    ) as runtime:
        return runtime.call(method, params)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect coding sessions stored in JSONL log files.",
        usage="ct [flags] <command> [args]",
        epilog=EPILOG,
        formatter_class=GhFormatter,
    )
    add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in REGISTRARS:
        register(subparsers)
    return parser


def _render_payload(args: argparse.Namespace, payload: Any) -> str:
    plugin_renderer = getattr(args, "_render_payload", None)
    if callable(plugin_renderer):
        return plugin_renderer(args, payload)

    method = getattr(args, "_method", None)
    if selected_output(args) == "json":
        if method:
            compact = compact_payload(method, payload)
            compact = service_contract(method).validate_public_response(compact)
            return json_text(compact)
        return json_text(payload)

    renderer = getattr(args, "_renderer", None)
    if callable(renderer):
        return renderer(payload)

    return json_text(compact_payload(method, payload)) if method else json_text(payload)


def _command_path(args: argparse.Namespace) -> str:
    command = getattr(args, "command", None) or ""
    action = getattr(args, "action", None)
    if action:
        return f"{command}.{action}"
    plugin_name = getattr(args, "_plugin_name", None)
    if plugin_name:
        return f"plugin.{plugin_name}"
    return command or "unknown"


def _payload_session_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("root_session_id", "session_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _payload_vendor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    vendor = payload.get("vendor")
    if isinstance(vendor, str):
        return vendor
    return None


def _invocation_log_path() -> Path | None:
    if os.environ.get("CT_TELEMETRY", "").strip().lower() in _TELEMETRY_DISABLED:
        return None
    return Path.home() / ".coding-trajectory" / "invocations.jsonl"


def _json_default(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.isoformat()
    return str(value)


def _write_invocation(record: dict[str, Any]) -> None:
    path = _invocation_log_path()
    if path is None:
        return
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n"
    except (TypeError, ValueError):
        return
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, line.encode("utf-8"))
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    with debug.debug_scope() as debug_ctx:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        plugin_exit = dispatch_plugin_argv(raw_args)
        if plugin_exit is not None:
            return plugin_exit

        parser = _build_parser()
        args = parser.parse_args(raw_args)

        start = time.monotonic()
        ok = True
        error_message: str | None = None
        payload: Any = None

        try:
            plugin_handler = getattr(args, "_plugin_handler", None)
            payload = plugin_handler(args) if callable(plugin_handler) else _dispatch(args)
            if isinstance(payload, int):
                return payload
        except (ResourceNotFoundError, DocumentError) as exc:
            ok = False
            error_message = str(exc)
            print(json.dumps({"error": {"message": error_message}}, indent=2), file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover - defensive CLI fallback
            ok = False
            error_message = str(exc)
            print(json.dumps({"error": {"message": error_message}}, indent=2), file=sys.stderr)
            return 1
        finally:
            _write_invocation(
                {
                    "ts": _dt.datetime.now(_dt.timezone.utc),
                    "ct_version": _cli_version(),
                    "cwd": str(Path.cwd()),
                    "cmd": _command_path(args),
                    "method": getattr(args, "_method", None),
                    "session_id": _payload_session_id(payload),
                    "vendor": _payload_vendor(payload),
                    "ok": ok,
                    "error": error_message,
                    "ms": round((time.monotonic() - start) * 1000),
                    "warnings": debug_ctx.as_records(),
                }
            )

        output = _render_payload(args, payload)
        if selected_output(args) == "json":
            print(output)
        else:
            print(render_markdown_for_terminal(output))
        return 0


def _cli_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return None
    try:
        return version("coding-trajectory")
    except PackageNotFoundError:
        return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
