"""Tool and shell event labeling for Context Window trajectory events."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from datahub_plugin.projections.context_window.models import (
    CategoryKey,
    Confidence,
    ContextEvent,
    CostEvidence,
    TokenEvidence,
)


def _tool_events_by_turn(
    tool_usage: dict[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, list[list[ContextEvent]]]:
    by_turn: dict[str, list[list[ContextEvent]]] = {}
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        if session_id and str(item.get("session_id") or "") != session_id:
            continue
        turn_id = _optional_text(item.get("turn_id"))
        if turn_id is None:
            continue
        by_turn.setdefault(turn_id, []).append(_tool_item_events(item, index=index))
    return by_turn


def _tool_item_events(
    item: dict[str, Any],
    *,
    index: int,
) -> list[ContextEvent]:
    item_id = str(item.get("item_id") or f"tool_item_{index}")
    tool = str(item.get("tool_name") or "Tool")
    attribution = (
        item.get("token_attribution")
        if isinstance(item.get("token_attribution"), dict)
        else {}
    )
    real_cost = (
        item.get("allocated_real_token_cost")
        if isinstance(item.get("allocated_real_token_cost"), dict)
        else {}
    )
    input_tokens = _optional_int(attribution.get("tool_input_tokens")) or 0
    output_tokens = _optional_int(attribution.get("tool_output_tokens")) or 0
    total_tokens = input_tokens + output_tokens
    real_total_tokens = _optional_int(real_cost.get("processed_tokens"))
    output_chars = _optional_int(item.get("output_chars")) or 0
    output_original_tokens = _optional_int(item.get("output_original_tokens"))
    input_summary = _optional_text(item.get("input_summary")) or f"{tool} input"
    detail_ref = {
        "item_id": item_id,
        "session_id": str(item.get("session_id") or ""),
        "turn_id": str(item.get("turn_id") or ""),
        "tool_name": tool,
        "tool_bucket": _tool_bucket_key(input_summary, tool),
        "tool_input_tokens": str(input_tokens),
        "tool_output_tokens": str(output_tokens),
    }
    for source_key, detail_key in (
        ("prompt_tokens", "allocated_prompt_tokens"),
        ("uncached_prompt_tokens", "allocated_uncached_prompt_tokens"),
        ("cached_prompt_tokens", "allocated_cached_prompt_tokens"),
        ("cache_write_tokens", "allocated_cache_write_tokens"),
        ("completion_tokens", "allocated_completion_tokens"),
        ("reasoning_tokens", "allocated_reasoning_tokens"),
        ("processed_tokens", "allocated_processed_tokens"),
    ):
        value = _optional_int(real_cost.get(source_key))
        if value is not None:
            detail_ref[detail_key] = str(value)
    if real_cost.get("allocation_method"):
        detail_ref["allocated_token_method"] = str(real_cost["allocation_method"])
    estimated_cost = _cost_evidence_from_estimate(item.get("estimated_cost"))
    if estimated_cost:
        detail_ref["estimated_cost_usd"] = str(estimated_cost.value_usd)
    status = _optional_text(item.get("status"))
    if status:
        detail_ref["status"] = status

    label = _tool_event_label(tool, input_summary)
    summary_bits = [input_summary, f"{output_chars} output chars"]
    if real_total_tokens is not None:
        summary_bits.append(f"{real_total_tokens} allocated real tokens")
    if output_original_tokens is not None:
        summary_bits.append(f"{output_original_tokens} observed output tokens")
    if item.get("output_truncated"):
        summary_bits.append("output truncated")

    output_confidence = _tool_output_confidence(attribution.get("content_confidence"))
    combined_confidence = _combined_tool_confidence(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        output_confidence=output_confidence,
    )
    return [
        ContextEvent(
            id=f"tool:{item_id}",
            group="turn",
            turn_id=detail_ref["turn_id"],
            category=_tool_category(tool),
            label=label,
            summary=", ".join(summary_bits),
            tokens=TokenEvidence(
                value=total_tokens,
                confidence=combined_confidence,
                source="ct session.tool_usage:tool_input_tokens + tool_output_tokens",
            ),
            source="ct session.tool_usage:tool_items",
            confidence=combined_confidence,
            detail_ref=detail_ref,
            terminal_visible=True,
            estimated_cost=estimated_cost,
        ),
    ]


def _combined_tool_confidence(
    *,
    input_tokens: int,
    output_tokens: int,
    output_confidence: Confidence,
) -> Confidence:
    if output_tokens == 0:
        return "estimated_tokens" if input_tokens else "structural"
    if input_tokens == 0:
        return output_confidence
    return "estimated_tokens"


def _tool_output_confidence(value: Any) -> Confidence:
    if value == "observed_tool_output_token_count":
        return "exact_usage"
    if value == "no_visible_content":
        return "structural"
    return "estimated_tokens"


def _tool_category(tool: str) -> CategoryKey:
    normalized = tool.lower()
    if any(
        term in normalized
        for term in (
            "todo",
            "subagent",
            "handoff",
            "update_plan",
            "edit",
            "write",
            "apply_patch",
        )
    ):
        return "agent"
    if normalized in {"read", "view"} or any(
        term in normalized for term in ("read_file", "readfile", "read_many_files")
    ):
        return "files"
    return "output"


def _tool_bucket_key(input_summary: str, tool: str) -> str:
    lower = input_summary.lower()
    normalized_tool = tool.lower()
    if "apply_patch" in normalized_tool:
        return "edits"
    if normalized_tool == "reasoning":
        return "reasoning_items"
    if not _is_shell_tool(tool):
        return "other_tool"
    if "curl -fssl" in lower and "espn.com/soccer/" in lower and "| rg" in lower:
        return "raw_html_scrape"
    if lower.startswith(("rg ", "rg -n")) or " rg -n " in lower:
        if (
            re.search(r"\s\.(?:$|\s)", lower)
            or "src aws packages readme" in lower
            or "docs" in lower
            or "/memories/" in lower
            or "world cup readiness|readiness" in lower
            or "source-evidence|research|aws smoke" in lower
            or "limit|limit|default_event_limit" in lower
        ):
            return "broad_search"
        return "targeted_search"
    if lower.startswith(("sed ", "nl ", "cat ")):
        return "file_read_shell"
    if any(term in lower for term in ["git status", "git diff", "git log"]):
        return "git_inspection"
    if any(
        term in lower
        for term in [
            "aws batch",
            " aws iam ",
            " aws sts ",
            "wrangler d1",
            "tt research",
            "curl -fss https://trailtrading-research-api",
        ]
    ):
        return "cloud_state_check"
    if any(
        term in lower
        for term in ["py_compile", "bun run check", "diff --check", "ruby -e"]
    ):
        return "validation"
    if any(term in lower for term in ["git add", "git commit"]):
        return "git_write"
    return "other_exec"


def _tool_event_label(tool: str, input_summary: str) -> str:
    normalized = tool.lower()
    if "apply_patch" in normalized:
        target = _patch_target(input_summary)
        return f"Edit {target}" if target else "Edit files"
    if any(term in normalized for term in ("edit", "write")):
        target = _path_title(input_summary)
        action = "Write" if "write" in normalized else "Edit"
        return f"{action} {target}" if target else f"{action} files"
    if any(term in normalized for term in ("todo", "update_plan")):
        return "Update plan"
    if any(term in normalized for term in ("subagent", "handoff")):
        return _compact_title(tool.replace("_", " ").title())
    if _is_shell_tool(tool):
        return _shell_event_label(input_summary)
    if normalized in {"read", "view"} or any(
        term in normalized for term in ("read_file", "readfile", "read_many_files")
    ):
        target = _path_title(input_summary)
        return f"Read {target}" if target else "Read files"
    if any(term in normalized for term in ("search", "grep")):
        query = _search_query_title(input_summary)
        return f"grep {_quote_title(query)}" if query else "Search output"
    if any(term in normalized for term in ("list", "glob")):
        return "File listing output"
    return _compact_title(tool.replace("_", " ").strip().title() or "Tool")


def _is_shell_tool(tool: str) -> bool:
    return tool in {
        "bash",
        "Bash",
        "exec_command",
        "run_shell_command",
        "shell",
        "write_stdin",
    }


def _shell_event_label(command: str) -> str:
    primary = _primary_shell_stage(command)
    tokens = _safe_split(primary)
    head = _command_head(tokens)
    if head in {"rg", "grep", "ag", "ack", "rga"}:
        if any(token in {"--files", "-l", "--files-with-matches"} for token in tokens):
            return "File listing output"
        query = _grep_query(tokens, head)
        return f"grep {_quote_title(query)}" if query else "Search output"
    if head in {"ls", "find", "fd", "tree", "eza", "exa"}:
        return "File listing output"
    if head in {"cat", "bat", "head", "tail", "less", "more", "nl", "sed"}:
        target = _shell_path_arg(tokens, head)
        return f"Read {_path_title(target)}" if target else "Read command output"
    if head in {"apply_patch", "applypatch"}:
        target = _patch_target(command)
        return f"Edit {target}" if target else "Edit files"
    short = _compact_command(primary)
    return f"{short} output" if short else "Command output"


def _primary_shell_stage(command: str) -> str:
    for separator in ("&&", "||", ";", "\n"):
        if separator in command:
            parts = [part.strip() for part in command.split(separator) if part.strip()]
            informative = next(
                (
                    part
                    for part in parts
                    if _command_head(_safe_split(part))
                    in {"rg", "grep", "sed", "cat", "ls", "find", "fd"}
                ),
                None,
            )
            return informative or parts[0]
    return command.strip()


def _safe_split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _command_head(tokens: list[str]) -> str:
    if not tokens:
        return ""
    index = 0
    while (
        index < len(tokens)
        and "=" in tokens[index]
        and not tokens[index].startswith("-")
    ):
        index += 1
    if index < len(tokens) and tokens[index] in {
        "uv",
        "poetry",
        "pdm",
        "pipenv",
        "npx",
        "bunx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
    }:
        index += 1
        while index < len(tokens) and tokens[index] in {
            "run",
            "exec",
            "dlx",
            "tool",
            "task",
        }:
            index += 1
    if (
        index + 2 < len(tokens)
        and tokens[index] in {"python", "python3"}
        and tokens[index + 1] == "-m"
    ):
        return os.path.basename(tokens[index + 2].lower())
    return os.path.basename(tokens[index].lower()) if index < len(tokens) else ""


def _grep_query(tokens: list[str], head: str) -> str | None:
    saw_head = False
    skip_next = False
    flag_value_options = {
        "-A",
        "-B",
        "-C",
        "-e",
        "-f",
        "-g",
        "--glob",
        "-m",
        "--max-count",
        "-t",
        "--type",
        "--type-not",
        "-T",
        "-r",
        "--replace",
        "--include",
        "--exclude",
        "--exclude-dir",
    }
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in flag_value_options:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _shell_path_arg(tokens: list[str], head: str) -> str | None:
    saw_head = False
    skip_next = False
    for token in tokens:
        if not saw_head:
            if os.path.basename(token) == head:
                saw_head = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in {"-n", "-e"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _patch_target(text: str) -> str | None:
    for marker in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
        if marker in text:
            tail = text.split(marker, 1)[1]
            return _path_title(tail.splitlines()[0].strip())
    return _path_title(text) if "/" in text else None


def _path_title(path: str | None) -> str | None:
    if not path:
        return None
    cleaned = path.strip().strip("'\"")
    if not cleaned:
        return None
    return os.path.basename(cleaned.rstrip("/")) or cleaned


def _search_query_title(text: str) -> str | None:
    if ":" in text:
        text = text.split(":", 1)[-1]
    stripped = text.strip().strip("'\"")
    return stripped or None


def _quote_title(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{_compact_title(escaped, limit=48)}"'


def _compact_command(command: str, *, limit: int = 48) -> str:
    return _compact_title(" ".join(command.split()), limit=limit)


def _compact_title(value: str, *, limit: int = 72) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _confidence(value: Any, *, fallback: Confidence) -> Confidence:
    if value in {
        "exact_usage",
        "exact_text",
        "estimated_tokens",
        "structural",
        "unknown",
    }:
        return value
    return fallback


def _cost_evidence_from_estimate(estimate: Any) -> CostEvidence | None:
    """Project a core-emitted ``estimated_cost`` dict to the datahub's
    ``CostEvidence``. ``None`` when the model was unknown to the pricing
    catalog (no rule matched) — cost is omitted rather than reported as 0.
    """
    if not isinstance(estimate, dict) or estimate.get("value_usd") is None:
        return None
    return CostEvidence(
        value_usd=estimate.get("value_usd"),
        confidence=estimate.get("confidence") or "estimated",
        source=str(estimate.get("source") or "ct pricing"),
        effective_date=estimate.get("effective_date"),
    )
