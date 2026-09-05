"""Command-line interface for reading coding session graph data."""

from __future__ import annotations

import argparse
import datetime as _dt
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

from coding_trajectory import debug
from coding_trajectory.contracts import service_contract
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory_cli._shared import (
    GhFormatter,
    compact_payload,
    json_text,
    render_markdown_for_terminal,
    selected_output,
)
from coding_trajectory_cli.commands import REGISTRARS, dispatch_plugin_argv
from coding_trajectory_cli.commands.api import _runtime
from coding_trajectory_cli.outcome import command_path, normalize_handler_result
from coding_trajectory_cli.telemetry import write_invocation_record

EPILOG = """\
NOTE
  Use `ct project sessions` to choose the SESSION_ID required by session and
  session graph analysis commands. Evidence reads require a published session
  scope even when selecting explicit --event-id values.
"""


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    method: str = args._method
    params: dict[str, Any] = args._params(args)
    with _runtime(args) as runtime:
        return runtime.call(method, params)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect published coding sessions in the canonical Supabase workspace.",
        usage="ct <command> [args]",
        epilog=EPILOG,
        formatter_class=GhFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in REGISTRARS:
        register(subparsers)
    return parser


def _render_payload(args: argparse.Namespace, payload: Any) -> str:
    method = getattr(args, "_method", None)
    if selected_output(args) == "json":
        if method:
            compact = compact_payload(method, payload)
            compact = service_contract(method).validate_cli_response(compact)
            return json_text(compact)
        return json_text(payload)

    renderer = getattr(args, "_renderer", None)
    if callable(renderer):
        try:
            if len(inspect.signature(renderer).parameters) >= 2:
                return renderer(payload, args)
        except (TypeError, ValueError):
            pass
        return renderer(payload)

    return json_text(compact_payload(method, payload)) if method else json_text(payload)


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


def _record_invocation(
    *,
    command: str,
    method: str | None,
    payload: Any,
    exit_code: int,
    ok: bool,
    error: str | None,
    started_at: float,
    warnings: list[dict[str, Any]],
) -> None:
    write_invocation_record(
        {
            "ts": _dt.datetime.now(_dt.timezone.utc),
            "ct_version": _cli_version(),
            "cwd": str(Path.cwd()),
            "cmd": command,
            "method": method,
            "session_id": _payload_session_id(payload),
            "vendor": _payload_vendor(payload),
            "exit_code": exit_code,
            "ok": ok,
            "error": error,
            "ms": round((time.monotonic() - started_at) * 1000),
            "warnings": warnings,
        }
    )


def main(argv: list[str] | None = None) -> int:
    with debug.debug_scope() as debug_ctx:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        start = time.monotonic()
        plugin_dispatch = dispatch_plugin_argv(raw_args)
        if plugin_dispatch is not None:
            outcome = plugin_dispatch.outcome
            payload = outcome.payload if outcome.has_payload else None
            _record_invocation(
                command=plugin_dispatch.command,
                method=None,
                payload=payload,
                exit_code=outcome.exit_code,
                ok=outcome.ok,
                error=outcome.error,
                started_at=start,
                warnings=debug_ctx.as_records(),
            )
            return outcome.exit_code

        parser = _build_parser()
        args = parser.parse_args(raw_args)
        command_name = command_path(args)
        method = getattr(args, "_method", None)
        exit_code = 0
        ok = True
        error_message: str | None = None
        payload: Any = None

        try:
            plugin_handler = getattr(args, "_plugin_handler", None)
            result = (
                plugin_handler(args) if callable(plugin_handler) else _dispatch(args)
            )
            outcome = normalize_handler_result(args, result)
            exit_code = outcome.exit_code
            ok = outcome.ok
            error_message = outcome.error
            if outcome.has_payload:
                payload = outcome.payload
        except (ResourceNotFoundError, DocumentError) as exc:
            exit_code = 1
            ok = False
            error_message = str(exc)
            print(
                json.dumps({"error": {"message": error_message}}, indent=2),
                file=sys.stderr,
            )
            return exit_code
        except Exception as exc:  # pragma: no cover - defensive CLI fallback
            exit_code = 1
            ok = False
            error_message = str(exc)
            print(
                json.dumps({"error": {"message": error_message}}, indent=2),
                file=sys.stderr,
            )
            return exit_code
        finally:
            _record_invocation(
                command=command_name,
                method=method,
                payload=payload,
                exit_code=exit_code,
                ok=ok,
                error=error_message,
                started_at=start,
                warnings=debug_ctx.as_records(),
            )

        if payload is None:
            return exit_code

        output = _render_payload(args, payload)
        if selected_output(args) == "json":
            print(output)
        else:
            print(render_markdown_for_terminal(output))
        return exit_code


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
