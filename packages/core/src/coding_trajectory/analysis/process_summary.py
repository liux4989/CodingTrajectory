"""Readable polling descriptions derived from the resolved command evidence."""

from __future__ import annotations

import ast
import re
from typing import Literal

from coding_trajectory.analysis.projection_utils import truncate_text_preview
from coding_trajectory.analysis.tool_arguments import sanitize_argument_preview
from coding_trajectory.ingestion.execution_models import ProcessTrace


def process_summary(
    trace: ProcessTrace,
) -> tuple[str, Literal["process_command", "process_id"]]:
    if trace.poll_arguments.get("chars"):
        return f"Send input to process {trace.process_id}", "process_id"
    if trace.resolution != "resolved" or not trace.arguments:
        return f"Poll process {trace.process_id}", "process_id"
    command = trace.arguments.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return f"Poll process {trace.process_id}", "process_id"
    # This is a summary of the recorded program's purpose, not evidence that
    # its assertions ran or passed. Parse literal call arguments without eval.
    heredoc = re.search(
        r"\bpython(?:3)?\s+[^\n]*<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1(?:\n|$)",
        command,
        re.DOTALL,
    )
    if heredoc:
        try:
            tree = ast.parse(heredoc.group(2))
        except SyntaxError:
            tree = None
        if tree is not None:
            subjects = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                values = [
                    arg.value
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    else None
                    for arg in node.args
                ]
                if values[:2] == ["session", "overview"]:
                    subjects.add("overview")
                if values[:3] == ["session", "graph", "overview"]:
                    subjects.add("graph overview")
            if subjects and any(
                isinstance(node, ast.Assert) for node in ast.walk(tree)
            ):
                subject = (
                    "overview and graph"
                    if subjects == {"overview", "graph overview"}
                    else " and ".join(sorted(subjects))
                )
                return f"Collect {subject} validation results", "process_command"
    safe_command = sanitize_argument_preview(command) or ""
    return "Collect results: " + truncate_text_preview(
        safe_command, max_len=260
    ), "process_command"
