"""Shared CLI parser, rendering, and output helpers."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from coding_trajectory.analysis.projection_utils import truncate_text_preview

OUTPUT_CHOICES = ("markdown", "json")
TERMINAL_LINE_LIMIT = 140
UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


class GhFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that matches the gh/git style: ALL CAPS sections, USAGE prefix."""

    def start_section(self, heading: str | None) -> None:
        renames = {"positional arguments": "ARGUMENTS", "options": "FLAGS"}
        super().start_section(renames.get(heading or "", heading or ""))

    def add_arguments(self, actions: object) -> None:
        if any(isinstance(a, argparse._SubParsersAction) for a in actions):  # type: ignore[attr-defined]
            self._current_section.heading = "CORE COMMANDS"
        super().add_arguments(actions)  # type: ignore[arg-type]

    def format_help(self) -> str:
        text = super().format_help()
        text = re.sub(r"^usage:", "USAGE\n ", text, flags=re.MULTILINE)
        text = re.sub(r"^([A-Z][A-Z ]+):$", r"\1", text, flags=re.MULTILINE)
        lines = text.split("\n")
        flags_start = next((i for i, ln in enumerate(lines) if ln == "FLAGS"), None)
        if flags_start is not None:
            flags_end = len(lines)
            for i in range(flags_start + 1, len(lines)):
                if lines[i] and re.match(r"^[A-Z][A-Z ]+$", lines[i]):
                    flags_end = i
                    break
            if flags_end < len(lines):
                flags_block = lines[flags_start:flags_end]
                remaining = lines[:flags_start] + lines[flags_end:]
                while remaining and not remaining[-1].strip():
                    remaining.pop()
                text = "\n".join(remaining) + "\n\n" + "\n".join(flags_block).rstrip() + "\n"
        return text


def add_session_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "session_id",
        metavar="SESSION_ID",
        nargs="?",
        default=None,
        help="Session ID to use as the session tree entry point. Omit to use the most-recent session.",
    )


def add_base_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "--format",
        "-o",
        dest="output_format",
        choices=OUTPUT_CHOICES,
        default=None,
        metavar="{" + ",".join(OUTPUT_CHOICES) + "}",
        help="Select stdout format.",
    )
    parser.set_defaults(global_scope=False)


def add_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "--format",
        "-o",
        dest="output_format",
        choices=OUTPUT_CHOICES,
        default=None,
        metavar="{" + ",".join(OUTPUT_CHOICES) + "}",
        help="Select stdout format.",
    )
    parser.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def add_params_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--params",
        dest="params_json",
        type=json_object_arg,
        default=None,
        metavar="JSON",
        help="Merge a JSON object into the command request. Explicit CLI flags override matching keys.",
    )


def add_metrics_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--extra-billing",
        action="store_true",
        default=None,
        help="Mark cost estimates as outside-plan/API billing instead of plan-usage estimates.",
    )


def add_agent_vendor_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-vendor",
        metavar="AGENT_VENDOR",
        dest="agent_vendor",
        default=None,
        help="Filter by agent vendor. Known values: claude_code, codex_cli, pi.",
    )


def add_turn_window_flags(parser: argparse.ArgumentParser, *, view_name: str) -> None:
    parser.add_argument(
        "--turns",
        dest="num_turns",
        type=positive_int,
        default=None,
        metavar="N",
        help=f"Limit each session {view_name} to its last N visible turns.",
    )
    parser.add_argument(
        "--drop-turns",
        dest="drop_turns",
        type=positive_int,
        default=None,
        metavar="K",
        help="Drop the last K visible turns, matching thread/rollback semantics.",
    )


def params_from_json(args: argparse.Namespace) -> dict[str, Any]:
    params = getattr(args, "params_json", None)
    return dict(params or {})


def json_object_arg(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def selected_output(args: argparse.Namespace) -> str:
    return getattr(args, "output_format", None) or getattr(args, "_default_output", "markdown")


def _strip_inline_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return re.sub(r"`([^`]*)`", r"\1", text)


def _normalize_terminal_text(text: str) -> str:
    text = _strip_inline_markdown(text)
    return UUID_PATTERN.sub(lambda match: match.group(0)[:8], text)


def _is_markdown_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _is_markdown_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|") and "|" in line.strip()[1:-1]


def _render_markdown_table(rows: list[str]) -> list[str]:
    parsed_rows = [
        [_normalize_terminal_text(cell.strip()) for cell in row.strip().strip("|").split("|")]
        for row in rows
        if not _is_markdown_table_separator(row)
    ]
    if not parsed_rows:
        return []
    column_count = max(len(row) for row in parsed_rows)
    widths = [0] * column_count
    for row in parsed_rows:
        row.extend([""] * (column_count - len(row)))
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return ["  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip() for row in parsed_rows]


def _trim_terminal_line(line: str, *, limit: int = TERMINAL_LINE_LIMIT) -> str:
    if len(line) <= limit:
        return line
    bracket_suffix = re.fullmatch(r"(\s*-\s+\S+\s+)(.+?)(\s+\[[^\]]+\])", line)
    if bracket_suffix:
        prefix, text, suffix = bracket_suffix.groups()
        text_limit = max(limit - len(prefix) - len(suffix), 16)
        return f"{prefix}{truncate_text_preview(text, max_len=text_limit)}{suffix}"
    label_value = re.fullmatch(r"(\s*[^:]{1,32}:\s+)(.+)", line)
    if label_value:
        prefix, text = label_value.groups()
        text_limit = max(limit - len(prefix), 16)
        return f"{prefix}{truncate_text_preview(text, max_len=text_limit)}"
    return truncate_text_preview(line, max_len=limit)


def render_markdown_for_terminal(markdown: str) -> str:
    """Render the small Markdown subset emitted by the CLI as readable terminal text."""

    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    in_code_block = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            index += 1
            continue

        if in_code_block:
            rendered.append(_normalize_terminal_text(line))
            index += 1
            continue

        if _is_markdown_table_row(line) and index + 1 < len(lines) and _is_markdown_table_separator(lines[index + 1]):
            table_rows = [line]
            index += 2
            while index < len(lines) and _is_markdown_table_row(lines[index]):
                table_rows.append(lines[index])
                index += 1
            rendered.extend(_render_markdown_table(table_rows))
            continue

        heading = re.fullmatch(r"#{1,6}\s+(.+)", stripped)
        if heading:
            rendered.append(_trim_terminal_line(_normalize_terminal_text(heading.group(1))))
        elif stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            rendered.append(_trim_terminal_line(f"Warning: {_normalize_terminal_text(quote)}"))
        else:
            rendered.append(_trim_terminal_line(_normalize_terminal_text(line)))
        index += 1

    return "\n".join(rendered).rstrip()


def drop_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def compact_usage(usage: dict[str, Any] | None, *, include_cost: bool = True) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    return drop_none(
        {
            "in": usage.get("input_tokens"),
            "cache": usage.get("cached_input_tokens"),
            "out": usage.get("output_tokens"),
            "reason": usage.get("reasoning_output_tokens"),
            "total": usage.get("total_tokens"),
            "cost": usage.get("cost_usd") if include_cost else None,
        }
    ) or None


def compact_request(request: Any) -> Any:
    if not isinstance(request, dict):
        return request
    text = request.get("content") or request.get("summary") or request.get("text")
    return drop_none(
        {
            "text": text,
            "src": request.get("source"),
            "type": request.get("type") if request.get("type") not in {None, "message"} else None,
        }
    ) or None


def compact_activity(activity: Any) -> Any:
    if not isinstance(activity, dict):
        return activity
    if "tool" in activity:
        compact = {
            "tool": activity.get("tool"),
            "n": activity.get("count"),
            "status": activity.get("status"),
        }
        for key in ("cmd", "path", "query", "url", "text"):
            if activity.get(key) is not None:
                compact[key] = activity.get(key)
        for key in ("paths", "queries", "urls", "targets"):
            if activity.get(key) is not None:
                compact[key] = activity.get(key)
        if compact.get("n") == 1:
            compact.pop("n", None)
        return drop_none(compact)
    if "text" in activity:
        return {"text": activity.get("text")}
    if "teammate_summary" in activity:
        return {"team": activity.get("teammate_summary")}
    return activity


def compact_relationship(relationship: Any) -> Any:
    if not isinstance(relationship, dict):
        return relationship
    if relationship.get("role") == "main":
        return drop_none({"role": "main", "forks": relationship.get("forked_session_ids")})
    return drop_none(
        {
            "type": relationship.get("relationship"),
            "parent": relationship.get("parent_session_id"),
            "forks": relationship.get("forked_session_ids"),
        }
    ) or None


def compact_context_category(category: Any) -> Any:
    if not isinstance(category, dict):
        return category
    return drop_none(
        {
            "k": category.get("key"),
            "l": category.get("label"),
            "t": category.get("tokens"),
            "p": category.get("percent"),
            "d": category.get("details") or None,
            "c": [compact_context_category(child) for child in category.get("children") or []] or None,
        }
    )


def compact_payload(method: str, payload: Any) -> Any:
    if method == "project.list" and isinstance(payload, dict):
        items = payload.get("items") or {}
        return {
            "items": {
                name: drop_none({"p": item.get("path"), "v": item.get("vendors"), "sessions": item.get("sessions")})
                for name, item in items.items()
                if isinstance(item, dict)
            }
        }

    if method == "project.sessions" and isinstance(payload, dict):
        return {
            "items": [
                drop_none(
                    {
                        "id": item.get("root_session_id"),
                        "title": item.get("title"),
                        "v": item.get("vendors"),
                        "sessions": item.get("session_ids"),
                    }
                )
                for item in payload.get("items") or []
                if isinstance(item, dict)
            ]
        }

    if method == "session.overview" and isinstance(payload, dict):
        return {
            "id": payload.get("root_session_id"),
            "sessions": [
                drop_none(
                    {
                        "id": session.get("session_id"),
                        "rel": compact_relationship(session.get("relationship")),
                        "v": session.get("vendor"),
                        "status": session.get("status"),
                        "agent": session.get("agent_name"),
                        "cwd": session.get("cwd"),
                        "turns": [
                            drop_none(
                                {
                                    "id": turn.get("turn_id"),
                                    "status": turn.get("status"),
                                    "req": compact_request(turn.get("user_request")),
                                    "act": [compact_activity(activity) for activity in turn.get("activity") or []] or None,
                                    "team": turn.get("teammate_summary"),
                                    "steps": ((turn.get("refs") or {}).get("step_ids") if isinstance(turn.get("refs"), dict) else None),
                                }
                            )
                            for turn in session.get("turns") or []
                            if isinstance(turn, dict)
                        ],
                    }
                )
                for session in payload.get("sessions") or []
                if isinstance(session, dict)
            ],
        }

    if method == "session.usage" and isinstance(payload, dict):
        return drop_none(
            {
                "id": payload.get("session_id"),
                "extra_billing": payload.get("extra_billing"),
                "usage": compact_usage(payload.get("total_usage")),
                "cost": payload.get("cost_usd"),
                "turns": [
                    drop_none(
                        {
                            "id": turn.get("turn_id"),
                            "session": turn.get("session_id"),
                            "usage": compact_usage(turn.get("usage")),
                            "cost": turn.get("cost_usd"),
                            "act": [
                                drop_none(
                                    {
                                        "kind": item.get("category"),
                                        "usage": compact_usage(item.get("usage")),
                                        "cost": item.get("cost_usd"),
                                    }
                                )
                                for item in turn.get("activity_usage") or []
                                if isinstance(item, dict)
                            ]
                            or None,
                        }
                    )
                    for turn in payload.get("turns") or []
                    if isinstance(turn, dict)
                ],
                "warn": payload.get("warnings") or None,
            }
        )

    if method == "session.stats" and isinstance(payload, dict):
        model = payload.get("model") or {}
        ctx = payload.get("context_window") or {}
        runtime = payload.get("runtime") or {}
        messages = payload.get("messages") or {}
        quota = payload.get("quota") or {}
        return drop_none(
            {
                "id": payload.get("root_session_id"),
                "v": payload.get("vendor"),
                "model": drop_none({"name": model.get("name"), "ctx": model.get("context_window_tokens")}) or None,
                "ctx": drop_none(
                    {
                        "used": ctx.get("used_tokens"),
                        "pct": ctx.get("used_percent"),
                        "cats": [compact_context_category(item) for item in ctx.get("categories") or []] or None,
                    }
                )
                or None,
                "rt": drop_none(
                    {
                        "status": runtime.get("status"),
                        "start": runtime.get("started_at"),
                        "end": runtime.get("ended_at"),
                        "dur_s": runtime.get("duration_seconds"),
                        "turns": runtime.get("turns"),
                        "steps": runtime.get("model_steps"),
                        "tools": runtime.get("tool_calls"),
                        "ftools": runtime.get("failed_tool_calls") or None,
                        "subs": runtime.get("subagent_sessions"),
                        "compactions": runtime.get("compactions"),
                    }
                )
                or None,
                "msg": drop_none(
                    {
                        "user": messages.get("user"),
                        "assistant": messages.get("assistant"),
                        "developer": messages.get("developer"),
                        "tools": messages.get("tool_outputs"),
                        "reasoning": messages.get("reasoning_items"),
                        "compacted": messages.get("compacted_contexts"),
                    }
                )
                or None,
                "usage": compact_usage(payload.get("usage"), include_cost=False),
                "quota": drop_none(
                    {
                        "plan": quota.get("plan_type"),
                        "primary_pct": quota.get("primary_used_percent"),
                        "secondary_pct": quota.get("secondary_used_percent"),
                        "reset_at": quota.get("resets_at"),
                    }
                )
                or None,
                "warn": payload.get("warnings") or None,
            }
        )

    if method == "step.details" and isinstance(payload, list):
        return [
            drop_none(
                {
                    "id": item.get("step_id"),
                    "type": item.get("type"),
                    "ops": item.get("operations"),
                    "shape": item.get("shape"),
                    "events": item.get("event_ids"),
                }
            )
            for item in payload
            if isinstance(item, dict)
        ]

    if method == "event.detail" and isinstance(payload, dict):
        return drop_none(
            {
                "id": payload.get("event_id"),
                "session": payload.get("session_id"),
                "ts": payload.get("timestamp"),
                "type": payload.get("type"),
                "tool": payload.get("tool_call"),
                "llm": payload.get("llm"),
                "text": payload.get("text"),
            }
        )

    if method == "event.scan" and isinstance(payload, dict):
        return drop_none(
            {
                "id": payload.get("root_session_id"),
                "type": payload.get("type"),
                "matches": [
                    drop_none(
                        {
                            "id": item.get("event_id"),
                            "session": item.get("session_id"),
                            "ts": item.get("timestamp"),
                            "payload": item.get("payload"),
                        }
                    )
                    for item in payload.get("matches") or []
                    if isinstance(item, dict)
                ],
            }
        )

    return payload


def display_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    text = str(value or "")
    if "." in text:
        return text.rsplit(".", 1)[-1].lower()
    return text


def one_line(value: Any, *, limit: int = 96) -> str:
    return truncate_text_preview(value, max_len=limit)


def format_tokens(value: Any) -> str:
    try:
        tokens = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if abs(tokens) >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}m"
    if abs(tokens) >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def format_percent(value: Any) -> str:
    try:
        percent = float(value or 0)
    except (TypeError, ValueError):
        return ""
    return f"({percent:.1f}%)"


def format_duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_cost(value: Any) -> str:
    try:
        cost = float(value or 0)
    except (TypeError, ValueError):
        return "$0.00"
    return f"${cost:.4f}" if cost and cost < 0.01 else f"${cost:.2f}"


def render_usage_line(usage: dict[str, Any]) -> str:
    return (
        f"input {format_tokens(usage.get('input_tokens'))}  "
        f"cached {format_tokens(usage.get('cached_input_tokens'))}  "
        f"output {format_tokens(usage.get('output_tokens'))}  "
        f"reasoning {format_tokens(usage.get('reasoning_output_tokens'))}  "
        f"total {format_tokens(usage.get('total_tokens'))}  "
        f"cost {format_cost(usage.get('cost_usd'))}"
    )
