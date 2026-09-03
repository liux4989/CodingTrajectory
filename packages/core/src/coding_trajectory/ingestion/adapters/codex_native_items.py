"""Native Codex terminal items and legacy exec-wrapper reconstruction.

Each handler takes the adapter's ``_ParseState`` explicitly and mutates it in
place. Shared helpers and ``_PendingExecWrapper`` remain in ``codex.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from typing import Any

from coding_trajectory.ingestion.adapters.codex import (
    CodexAdapter,
    _as_non_empty_str,
    _codex_command_activity_source,
    _command_match_key,
    _has_explicit_exec_wrapper_result,
    _is_exec_syntax_error,
    _native_command_text,
    _PendingExecWrapper,
    _tool_result_status,
    _tool_status,
)
from coding_trajectory.ingestion.adapters.codex_exec_parser import (
    StaticExecInvocation,
)
from coding_trajectory.ingestion.common import parse_timestamp
from coding_trajectory.ingestion.models import ToolStatus, Vendor
from coding_trajectory.ingestion.transcript import TranscriptRecord

_CODEX_EXEC_STATIC_EXTRACTOR = "codex_exec_static_v2"
_NATIVE_EXEC_MATCH_GRACE_SECONDS = 2
_EXPANDED_EXEC_TOOL_NAME = "codex_exec_expanded"
_BACKGROUND_WAIT_TOOL_PREFIX = "codex_background_terminal_wait:"
_BACKGROUND_INTERACTION_TOOL_NAME = "codex_background_terminal_interaction"

# Moved signatures keep their original ``_ParseState`` spelling.
_ParseState = CodexAdapter._ParseState

def activity_data(
    *,
    outcome: str | None,
    fidelity: str,
    provenance: dict[str, Any],
    source: str = "agent",
    activity_kind: str | None = "command",
    wrapper_status: str | None = None,
) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "source": source,
        "fidelity": fidelity,
        "provenance": provenance,
    }
    if outcome is not None:
        activity["outcome"] = outcome
    if activity_kind is not None:
        activity["kind"] = activity_kind
    if wrapper_status is not None:
        activity["wrapper_status"] = wrapper_status
    return {"activity": activity}

def native_command_outcome(*, status: ToolStatus, exit_code: int | None) -> str:
    if exit_code == 0:
        return "succeeded"
    if exit_code is not None or status == ToolStatus.FAILED:
        return "failed"
    return "unknown"

def native_activity_outcome(*, status: ToolStatus) -> str:
    if status == ToolStatus.COMPLETED:
        return "succeeded"
    if status == ToolStatus.FAILED:
        return "failed"
    return "unknown"

def native_terminal_status(item: dict[str, Any], *, completed: bool) -> ToolStatus:
    """Normalize explicit native terminal status without wrapper inference."""

    if isinstance(item.get("success"), bool):
        return ToolStatus.COMPLETED if item["success"] else ToolStatus.FAILED
    return _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )

def native_item_timing(
    payload: dict[str, Any], timestamp: datetime
) -> tuple[datetime, datetime, dict[str, int]]:
    """Normalize optional rollout timing while retaining raw evidence.

    Historical terminal items often carry both epoch-millisecond endpoints
    on the enclosing event. The canonical item interval uses those values;
    provenance retains the original numbers for lossless inspection.
    """

    raw_started = payload.get("started_at_ms")
    raw_completed = payload.get("completed_at_ms")
    started_at = parse_timestamp(raw_started) or timestamp
    completed_at = parse_timestamp(raw_completed) or timestamp
    if completed_at < started_at:
        # Do not manufacture a negative canonical interval from malformed
        # telemetry. The raw values remain available in provenance below.
        started_at = completed_at
    timing = {
        key: value
        for key, value in (
            ("started_at_ms", raw_started),
            ("completed_at_ms", raw_completed),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if len(timing) == 2:
        duration = timing["completed_at_ms"] - timing["started_at_ms"]
        if duration >= 0:
            timing["duration_ms"] = duration
    return started_at, completed_at, timing

def pending_exec_wrapper_candidates(
    state: _ParseState,
    *,
    timestamp: datetime,
    predicate: Callable[[StaticExecInvocation], bool],
    turn_id: str | None = None,
) -> list[tuple[_PendingExecWrapper, int]]:
    candidates: list[tuple[_PendingExecWrapper, int]] = []
    for wrapper in state.pending_exec_wrappers.values():
        # A native item cannot precede the wrapper that lexically contains
        # it. When both records carry a turn id, crossing turns is likewise
        # not evidence of the same nested call.
        if timestamp < wrapper.started_at:
            continue
        if (
            turn_id is not None
            and wrapper.turn_id is not None
            and turn_id != wrapper.turn_id
        ):
            continue
        if (
            wrapper.closed
            and wrapper.completed_at is not None
            and (timestamp - wrapper.completed_at).total_seconds()
            > _NATIVE_EXEC_MATCH_GRACE_SECONDS
        ):
            continue
        for index, invocation in enumerate(wrapper.invocations):
            if index in wrapper.matched_native_indices:
                continue
            if predicate(invocation):
                candidates.append((wrapper, index))
    return candidates

def match_pending_exec_wrapper(
    state: _ParseState,
    *,
    command: str | None,
    timestamp: datetime,
    turn_id: str | None = None,
) -> tuple[_PendingExecWrapper, int] | None:
    key = _command_match_key(command)
    if key is None:
        return None
    candidates = pending_exec_wrapper_candidates(
        state,
        timestamp=timestamp,
        predicate=lambda invocation: _command_match_key(invocation.command) == key,
        turn_id=turn_id,
    )
    if len(candidates) != 1:
        return None
    wrapper, index = candidates[0]
    wrapper.matched_native_indices.add(index)
    if wrapper.closed and len(wrapper.matched_native_indices) == len(
        wrapper.invocations
    ):
        # A native terminal item may be persisted just after the wrapper
        # result. Once every child has authoritative evidence, suppress the
        # failed envelope exactly as we do when matching finished earlier.
        hide_expanded_exec_wrapper(wrapper)
    return wrapper, index

def match_pending_exec_wrapper_invocation(
    state: _ParseState,
    *,
    timestamp: datetime,
    predicate: Callable[[StaticExecInvocation], bool],
    turn_id: str | None = None,
) -> tuple[_PendingExecWrapper, int] | None:
    candidates = pending_exec_wrapper_candidates(
        state,
        timestamp=timestamp,
        predicate=predicate,
        turn_id=turn_id,
    )
    if len(candidates) != 1:
        return None
    wrapper, index = candidates[0]
    wrapper.matched_native_indices.add(index)
    if wrapper.closed and len(wrapper.matched_native_indices) == len(
        wrapper.invocations
    ):
        hide_expanded_exec_wrapper(wrapper)
    return wrapper, index

def hide_expanded_exec_wrapper(wrapper: _PendingExecWrapper) -> None:
    vendor_data = wrapper.call_record.data.setdefault("vendor_data", {})
    if not isinstance(vendor_data, dict):
        return
    vendor_data["activity"] = {
        "hidden_from_overview": True,
        "fidelity": "observed_wrapper",
        "reason": "static_exec_expansion",
        "child_count": len(wrapper.invocations),
        "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
    }
    # Compact retention strips vendor_data and wrapper input. Keep only an
    # opaque semantic marker on the canonical row so compact projections do
    # not resurrect this transport envelope as generic ``exec`` activity.
    wrapper.call_record.data["compact_tool_name"] = _EXPANDED_EXEC_TOOL_NAME


def _background_wait_tool_name(
    identity: tuple[str, str | int],
    *,
    wrapper_call_id: str,
    state: _ParseState,
) -> str:
    """Return an opaque grouping marker that does not encode the identity.

    Compact overview needs a stable equality key to coalesce same-terminal
    polling. The canonical input keeps the raw identifier only in trajectory
    retention. The compact token is derived from the first already-retained
    wrapper call id observed for that identity, never from the process/session
    value itself.
    """

    kind, value = identity
    key = (kind, str(value), state.activity_cell_epoch)
    token = state.background_terminal_group_tokens.get(key)
    if token is None:
        token = sha256(wrapper_call_id.encode()).hexdigest()
        state.background_terminal_group_tokens[key] = token
    return f"{_BACKGROUND_WAIT_TOOL_PREFIX}{token}"


def static_activity_input(invocation: StaticExecInvocation) -> Any:
    """Keep fallback activity useful without copying a full patch payload."""

    if invocation.method != "apply_patch":
        return invocation.input
    if isinstance(invocation.input, dict):
        reference = invocation.input.get("_static_reference")
        if isinstance(reference, str) and reference:
            return {"patch_reference": reference}
    return {"patch_reference": "literal"}

def _background_terminal_identity(
    invocation: StaticExecInvocation,
) -> tuple[str, str | int] | None:
    """Return the namespaced identity for a static ``write_stdin`` call."""

    if invocation.method != "write_stdin" or not isinstance(invocation.input, dict):
        return None
    if not isinstance(invocation.input.get("chars"), str):
        return None
    identities = [
        (key, invocation.input.get(key))
        for key in ("process_id", "session_id")
        if key in invocation.input
    ]
    if len(identities) != 1:
        return None
    key, value = identities[0]
    if (
        not isinstance(value, (str, int))
        or isinstance(value, bool)
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, int) and value <= 0)
    ):
        return None
    return key, value


def append_derived_exec_activities(
    wrapper: _PendingExecWrapper,
    *,
    state: _ParseState,
    timestamp: datetime,
    wrapper_status: ToolStatus,
    wrapper_output: Any,
    transcript: list[TranscriptRecord],
) -> None:
    """Emit typed fallback facts only where source evidence permits it."""

    # A single static invocation plus result payload is an observed wrapper
    # outcome. Multiple invocations remain ambiguous even when their shared
    # code cell completed, because the output cannot safely be attributed.
    observed_wrapper_result = (
        len(wrapper.invocations) == 1
        and wrapper_status == ToolStatus.COMPLETED
        and _has_explicit_exec_wrapper_result(wrapper_output)
    )

    for index, invocation in enumerate(wrapper.invocations):
        if index in wrapper.matched_native_indices:
            continue
        tool_call_id = f"{wrapper.call_id}:nested:{index}"
        input_data = static_activity_input(invocation)
        # ``Script completed`` can prove only that the outer JavaScript cell
        # returned. For stdin polling or interaction it is never proof of the
        # terminal-side outcome, even when other one-call wrapper forms carry
        # attributable result content.
        invocation_observed_result = (
            observed_wrapper_result and invocation.method != "write_stdin"
        )
        terminal_identity = _background_terminal_identity(invocation)
        background_poll = (
            terminal_identity is not None
            and isinstance(invocation.input, dict)
            and invocation.input.get("chars") == ""
        )
        background_interaction = (
            invocation.method == "write_stdin"
            and terminal_identity is not None
            and isinstance(invocation.input, dict)
            and isinstance(invocation.input.get("chars"), str)
            and bool(invocation.input["chars"])
        )
        semantic_tool_name = invocation.tool_name
        if background_poll and terminal_identity is not None:
            semantic_tool_name = _background_wait_tool_name(
                terminal_identity,
                wrapper_call_id=wrapper.call_id,
                state=state,
            )
        elif background_interaction:
            semantic_tool_name = _BACKGROUND_INTERACTION_TOOL_NAME
        activity_kind: str | None = None
        if background_poll:
            activity_kind = "background_terminal_wait"
        elif background_interaction:
            activity_kind = "background_terminal_interaction"
        elif invocation.item_kind == "command_execution":
            activity_kind = "command"
        activity_provenance: dict[str, Any] = {
            "parent_tool_call_id": wrapper.call_id,
            "parent_tool_name": "exec",
            "nested_method": invocation.method,
            "nested_index": index,
            "source_offset": invocation.source_offset,
            "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
            **({"wrapper_result_observed": True} if invocation_observed_result else {}),
        }
        if terminal_identity is not None:
            identity_key, identity_value = terminal_identity
            activity_provenance["terminal_identity"] = {
                "kind": identity_key,
                "value": identity_value,
            }
        activity_outcome: str | None = None
        if not (background_poll or background_interaction):
            activity_outcome = (
                "succeeded" if invocation_observed_result else "unknown"
            )
        activity = activity_data(
            outcome=activity_outcome,
            fidelity=(
                "observed_wrapper" if invocation_observed_result else "derived_static"
            ),
            activity_kind=activity_kind,
            wrapper_status=wrapper_status.value,
            provenance=activity_provenance,
        )
        if background_poll or background_interaction:
            # Completion of the JavaScript wrapper does not establish that
            # the terminal completed.  This merely exposes a wait observation
            # keyed by the terminal process for the overview projection.
            identity_key, identity_value = terminal_identity
            activity["activity"]["background_terminal_identity"] = (
                f"{identity_key}:{identity_value}"
            )
        record_data: dict[str, Any] = {
            "tool_name": invocation.tool_name,
            "compact_tool_name": (
                semantic_tool_name
                if semantic_tool_name != invocation.tool_name
                else None
            ),
            "tool_call_id": tool_call_id,
            "input": input_data,
            "status": (
                ToolStatus.COMPLETED.value
                if invocation_observed_result
                else ToolStatus.REQUESTED.value
            ),
            "item_kind": invocation.item_kind,
            "vendor_data": activity,
        }
        if invocation.command is not None:
            record_data["command"] = input_data
        activity_record = TranscriptRecord(
            sequence=len(transcript),
            timestamp=timestamp,
            vendor=Vendor.CODEX_CLI,
            role="assistant",
            kind="tool_call",
            data=record_data,
            fidelity="derived",
        )
        transcript.append(activity_record)
        wrapper.derived_records[index] = activity_record
        if invocation_observed_result:
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=timestamp,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": tool_call_id,
                        "tool_name": invocation.tool_name,
                        "output": wrapper_output,
                        "status": ToolStatus.COMPLETED.value,
                    },
                    fidelity="derived",
                )
            )

def handle_static_exec_wrapper_output(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
) -> None:
    call_id = _as_non_empty_str(payload.get("call_id"))
    if call_id is None:
        return
    wrapper = state.pending_exec_wrappers.get(call_id)
    if wrapper is None:
        return
    wrapper.closed = True
    wrapper.completed_at = timestamp
    wrapper_status = _tool_result_status(
        payload,
        payload.get("output"),
        exec_wrapper=True,
    )
    if wrapper_status == ToolStatus.FAILED and _is_exec_syntax_error(
        payload.get("output")
    ):
        # A parse failure occurs before any nested tools can execute. Keep
        # the raw failed exec row instead of hiding it behind a lexical
        # WebSearch/command fallback that never ran.
        vendor_data = wrapper.call_record.data.setdefault("vendor_data", {})
        if isinstance(vendor_data, dict):
            vendor_data.update(
                activity_data(
                    outcome="failed",
                    fidelity="observed_wrapper",
                    activity_kind=None,
                    provenance={
                        "tool_call_id": wrapper.call_id,
                        "reason": "exec_syntax_error_before_nested_execution",
                        "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                    },
                )
            )
        # A syntax error occurs before nested execution, so later unrelated
        # native items must not bind through the normal short grace window.
        state.pending_exec_wrappers.pop(call_id, None)
        return
    if wrapper_status == ToolStatus.FAILED:
        # The wrapper failed after parsing, but persisted evidence cannot say
        # whether any lexical child started. Keep the failed wrapper visible
        # instead of projecting a past-tense terminal wait or interaction.
        if len(wrapper.matched_native_indices) == len(wrapper.invocations):
            # Every lexical child is independently represented by a native
            # event, so the wrapper failure is only envelope evidence and
            # would duplicate the authoritative activities.
            hide_expanded_exec_wrapper(wrapper)
            return
        vendor_data = wrapper.call_record.data.setdefault("vendor_data", {})
        if isinstance(vendor_data, dict):
            vendor_data.update(
                activity_data(
                    outcome="failed",
                    fidelity="observed_wrapper",
                    activity_kind=None,
                    provenance={
                        "tool_call_id": wrapper.call_id,
                        "reason": "exec_wrapper_failed_nested_execution_unknown",
                        "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                    },
                )
            )
        return
    # The public wrapper is evidence, not a parallel visible action, once
    # every statically proven nested activity has its own canonical row.
    append_derived_exec_activities(
        wrapper,
        state=state,
        timestamp=timestamp,
        wrapper_status=wrapper_status,
        wrapper_output=payload.get("output"),
        transcript=transcript,
    )
    hide_expanded_exec_wrapper(wrapper)

def handle_native_command_execution(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return
    command_id = _as_non_empty_str(item.get("id"))
    if command_id is None:
        return
    command = _native_command_text(item.get("command"))
    exit_code = item.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    status = _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    turn_id = _as_non_empty_str(payload.get("turn_id"))
    outcome = native_command_outcome(status=status, exit_code=exit_code)
    matching = state.native_command_bindings.get(command_id)
    if matching is None:
        matching = match_pending_exec_wrapper(
            state,
            command=command,
            timestamp=completed_at,
            turn_id=turn_id,
        )
        if matching is not None:
            state.native_command_bindings[command_id] = matching

    if matching is not None:
        wrapper, index = matching
        derived = wrapper.derived_records.get(index)
        if derived is not None:
            # A late native lifecycle resolves a prior derived placeholder
            # without creating a duplicate command item.
            activity = activity_data(
                outcome=outcome,
                fidelity="derived_matched_native",
                provenance={
                    "parent_tool_call_id": wrapper.call_id,
                    "parent_tool_name": "exec",
                    "nested_index": index,
                    "native_command_id": command_id,
                    "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
                    **timing,
                },
            )
            derived.timestamp = started_at
            derived.data.update(
                {
                    "status": status.value,
                    "exit_code": exit_code,
                    "output": item.get("formatted_output") or item.get("stdout"),
                    "vendor_data": activity,
                }
            )
            if completed:
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=completed_at,
                        vendor=Vendor.CODEX_CLI,
                        role="tool",
                        kind="tool_result",
                        data={
                            "tool_call_id": derived.data["tool_call_id"],
                            "tool_name": "exec_command",
                            "output": item.get("formatted_output")
                            or item.get("stdout"),
                            "exit_code": exit_code,
                            "status": status.value,
                            "vendor_data": activity,
                        },
                    )
                )
            return

    command_input: dict[str, Any] = {}
    if command is not None:
        command_input["cmd"] = command
    cwd = _as_non_empty_str(item.get("cwd"))
    if cwd is not None:
        command_input["workdir"] = cwd.removeprefix("file://")
    native_data = activity_data(
        outcome=outcome,
        fidelity="observed_native",
        source=_codex_command_activity_source(item.get("source")),
        provenance={
            "native_command_id": command_id,
            "source": "event_msg.item_completed"
            if completed
            else "event_msg.item_started",
            "source_kind": _as_non_empty_str(item.get("source")),
            **timing,
        },
    )
    if command_id in state.native_command_ids:
        if completed:
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=completed_at,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": command_id,
                        "tool_name": "exec_command",
                        "output": item.get("formatted_output")
                        or item.get("stdout"),
                        "exit_code": exit_code,
                        "status": status.value,
                        "vendor_data": native_data,
                    },
                )
            )
        return

    state.native_command_ids.add(command_id)
    transcript.append(
        TranscriptRecord(
            sequence=len(transcript),
            timestamp=started_at,
            vendor=Vendor.CODEX_CLI,
            role="assistant",
            kind="tool_call",
            data={
                "tool_name": "exec_command",
                "tool_call_id": command_id,
                "input": command_input,
                "command": command_input,
                "status": (
                    ToolStatus.IN_PROGRESS.value if completed else status.value
                ),
                "item_kind": "command_execution",
                "vendor_data": native_data,
            },
            fidelity="derived" if completed else "observed",
        )
    )
    if completed:
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=completed_at,
                vendor=Vendor.CODEX_CLI,
                role="tool",
                kind="tool_result",
                data={
                    "tool_call_id": command_id,
                    "tool_name": "exec_command",
                    "output": item.get("formatted_output") or item.get("stdout"),
                    "exit_code": exit_code,
                    "status": status.value,
                    "vendor_data": native_data,
                },
            )
        )

def resolve_derived_exec_activity(
    wrapper: _PendingExecWrapper,
    index: int,
    *,
    timestamp: datetime,
    native_type: str,
    native_id: str,
    tool_name: str,
    item_kind: str,
    input_data: Any,
    status: ToolStatus,
    outcome: str,
    transcript: list[TranscriptRecord],
    completed: bool,
    output: Any = None,
    path: str | None = None,
    operation: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> bool:
    """Resolve a late native item onto its prior static wrapper child."""

    derived = wrapper.derived_records.get(index)
    if derived is None:
        return False
    canonical_started_at = started_at or timestamp
    canonical_completed_at = completed_at or timestamp
    activity = activity_data(
        outcome=outcome,
        fidelity="derived_matched_native",
        activity_kind=None,
        provenance={
            "parent_tool_call_id": wrapper.call_id,
            "parent_tool_name": "exec",
            "nested_index": index,
            "native_item_id": native_id,
            "native_item_type": native_type,
            "extractor": _CODEX_EXEC_STATIC_EXTRACTOR,
            **(provenance or {}),
        },
    )
    derived.timestamp = canonical_started_at
    derived.data.update(
        {
            "tool_name": tool_name,
            "input": input_data,
            "item_kind": item_kind,
            "status": status.value,
            "vendor_data": activity,
        }
    )
    if output is not None:
        derived.data["output"] = output
    if path is not None:
        derived.data["path"] = path
    if operation is not None:
        derived.data["operation"] = operation
    if not completed:
        return True
    transcript.append(
        TranscriptRecord(
            sequence=len(transcript),
            timestamp=canonical_completed_at,
            vendor=Vendor.CODEX_CLI,
            role="tool",
            kind="tool_result",
            data={
                "tool_call_id": derived.data["tool_call_id"],
                "tool_name": tool_name,
                "output": output,
                "status": status.value,
                "path": path,
                "operation": operation,
                "vendor_data": activity,
            },
        )
    )
    return True

def record_native_activity(
    *,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    timestamp: datetime,
    native_type: str,
    native_id: str,
    tool_name: str,
    item_kind: str,
    input_data: Any,
    status: ToolStatus,
    completed: bool,
    predicate: Callable[[StaticExecInvocation], bool],
    output: Any = None,
    path: str | None = None,
    operation: str | None = None,
    turn_id: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Project a non-command Codex ThreadItem and bind a static fallback."""

    native_key = (native_type, native_id)
    outcome = native_activity_outcome(status=status)
    canonical_started_at = started_at or timestamp
    canonical_completed_at = completed_at or timestamp
    matching = state.native_activity_bindings.get(native_key)
    if matching is None:
        matching = match_pending_exec_wrapper_invocation(
            state,
            timestamp=canonical_completed_at,
            predicate=predicate,
            turn_id=turn_id,
        )
        if matching is not None:
            state.native_activity_bindings[native_key] = matching

    if matching is not None:
        wrapper, index = matching
        if resolve_derived_exec_activity(
            wrapper,
            index,
            timestamp=timestamp,
            native_type=native_type,
            native_id=native_id,
            tool_name=tool_name,
            item_kind=item_kind,
            input_data=input_data,
            status=status,
            outcome=outcome,
            transcript=transcript,
            completed=completed,
            output=output,
            path=path,
            operation=operation,
            started_at=canonical_started_at,
            completed_at=canonical_completed_at,
            provenance=provenance,
        ):
            state.native_activity_ids.add(native_key)
            return

    activity = activity_data(
        outcome=outcome,
        fidelity="observed_native",
        activity_kind=None,
        provenance={
            "native_item_id": native_id,
            "native_item_type": native_type,
            "source": (
                "event_msg.item_completed"
                if completed
                else "event_msg.item_started"
            ),
            **(provenance or {}),
        },
    )
    if native_key in state.native_activity_ids:
        if completed:
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript),
                    timestamp=canonical_completed_at,
                    vendor=Vendor.CODEX_CLI,
                    role="tool",
                    kind="tool_result",
                    data={
                        "tool_call_id": native_id,
                        "tool_name": tool_name,
                        "output": output,
                        "status": status.value,
                        "path": path,
                        "operation": operation,
                        "vendor_data": activity,
                    },
                )
            )
        return

    state.native_activity_ids.add(native_key)
    transcript.append(
        TranscriptRecord(
            sequence=len(transcript),
            timestamp=canonical_started_at,
            vendor=Vendor.CODEX_CLI,
            role="assistant",
            kind="tool_call",
            data={
                "tool_name": tool_name,
                "tool_call_id": native_id,
                "input": input_data,
                "status": (
                    ToolStatus.IN_PROGRESS.value if completed else status.value
                ),
                "item_kind": item_kind,
                "path": path,
                "operation": operation,
                "vendor_data": activity,
            },
            fidelity="derived" if completed else "observed",
        )
    )
    if completed:
        transcript.append(
            TranscriptRecord(
                sequence=len(transcript),
                timestamp=canonical_completed_at,
                vendor=Vendor.CODEX_CLI,
                role="tool",
                kind="tool_result",
                data={
                    "tool_call_id": native_id,
                    "tool_name": tool_name,
                    "output": output,
                    "status": status.value,
                    "path": path,
                    "operation": operation,
                    "vendor_data": activity,
                },
            )
        )

def native_file_change_input(
    item: dict[str, Any],
) -> tuple[dict[str, Any], str | None, str | None]:
    changes = item.get("changes")
    rows: list[tuple[str, str | None]] = []
    if isinstance(changes, dict):
        # Historical rollout JSONL stores a path-indexed change object.
        for raw_path, raw_change in changes.items():
            if not isinstance(raw_path, str) or not raw_path:
                continue
            operation = (
                _as_non_empty_str(raw_change.get("type"))
                if isinstance(raw_change, dict)
                else None
            )
            rows.append((raw_path, operation))
    elif isinstance(changes, list):
        # The app-server ThreadItem schema uses a list of FileUpdateChange
        # objects with ``path`` and ``kind`` fields.
        for raw_change in changes:
            if not isinstance(raw_change, dict):
                continue
            raw_path = _as_non_empty_str(raw_change.get("path"))
            if raw_path is None:
                continue
            operation = _as_non_empty_str(
                raw_change.get("kind") or raw_change.get("type")
            )
            rows.append((raw_path, operation))
    else:
        return {}, None, None
    rows.sort(key=lambda row: row[0])
    paths = [path for path, _ in rows]
    operations = [operation for _, operation in rows if operation is not None]
    input_data: dict[str, Any] = {}
    if paths:
        input_data["paths"] = paths
    if operations:
        input_data["operations"] = operations
    return (
        input_data,
        paths[0] if len(paths) == 1 else None,
        operations[0] if len(operations) == 1 else None,
    )

def handle_native_file_change(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "FileChange":
        return
    native_id = _as_non_empty_str(item.get("id"))
    if native_id is None:
        return
    input_data, path, operation = native_file_change_input(item)
    output = {
        key: item[key]
        for key in ("stdout", "stderr")
        if _as_non_empty_str(item.get(key)) is not None
    } or None
    status = _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type="FileChange",
        native_id=native_id,
        tool_name="apply_patch",
        item_kind="file_change",
        input_data=input_data,
        status=status,
        completed=completed,
        predicate=lambda invocation: invocation.method == "apply_patch",
        output=output,
        path=path,
        operation=operation,
        turn_id=_as_non_empty_str(payload.get("turn_id")),
        started_at=started_at,
        completed_at=completed_at,
        provenance={"native_item_kind": "FileChange", **timing},
    )

def normalize_web_correlation_value(value: Any) -> str | None:
    """Normalize query/pattern values without evaluating wrapper code."""

    text = _as_non_empty_str(value)
    if text is None:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text or None

def static_web_search_queries(input_data: Any) -> set[str]:
    if not isinstance(input_data, dict):
        return set()
    queries = {
        query
        for query in (
            normalize_web_correlation_value(input_data.get("query")),
            normalize_web_correlation_value(input_data.get("q")),
        )
        if query is not None
    }
    for key in ("search_query", "image_query"):
        entries = input_data.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            query = normalize_web_correlation_value(
                entry.get("q") or entry.get("query")
            )
            if query is not None:
                queries.add(query)
    return queries

def static_web_operation_values(
    input_data: Any,
    operation: str,
    fields: tuple[str, ...],
) -> set[str]:
    if not isinstance(input_data, dict):
        return set()
    entries = input_data.get(operation)
    if not isinstance(entries, list):
        return set()
    values: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for field_name in fields:
            value = normalize_web_correlation_value(entry.get(field_name))
            if value is not None:
                values.add(value)
    return values

def extension_web_correlation_values(
    query: str | None,
    action: dict[str, Any],
) -> set[str]:
    """Collect every query/pattern value carried by an Extension item."""

    values = {
        value
        for value in (
            normalize_web_correlation_value(query),
            normalize_web_correlation_value(action.get("query")),
            normalize_web_correlation_value(action.get("pattern")),
            normalize_web_correlation_value(action.get("url")),
        )
        if value is not None
    }
    action_queries = action.get("queries")
    if isinstance(action_queries, list):
        for raw_query in action_queries:
            value = normalize_web_correlation_value(raw_query)
            if value is not None:
                values.add(value)
    return values

def static_web_matches_extension(
    invocation: StaticExecInvocation,
    *,
    action_type: str | None,
    query_values: set[str],
) -> bool:
    """Match a native Extension item only on compatible static evidence."""

    if invocation.method != "web__run" or not isinstance(invocation.input, dict):
        return False
    if action_type == "search":
        return bool(query_values & static_web_search_queries(invocation.input))
    if action_type == "openPage":
        return bool(
            query_values
            & static_web_operation_values(
                invocation.input, "open", ("ref_id", "url", "uri")
            )
        )
    if action_type == "findInPage":
        return bool(
            query_values
            & static_web_operation_values(
                invocation.input, "find", ("pattern",)
            )
        )
    if action_type == "other":
        # Codex's historical Extension schema does not expose a more
        # specific action for click/screenshot; timestamps + turn scope
        # still make a single pending matching wrapper unambiguous.
        return "click" in invocation.input or "screenshot" in invocation.input
    return False

def handle_native_web_search(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "WebSearch":
        return
    native_id = _as_non_empty_str(item.get("id"))
    query = _as_non_empty_str(item.get("query"))
    if native_id is None or query is None:
        return
    input_data: dict[str, Any] = {"query": query}
    if item.get("action") is not None:
        input_data["action"] = item["action"]
    output = {"results": item["results"]} if "results" in item else None
    status = _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type="WebSearch",
        native_id=native_id,
        tool_name="web_search",
        item_kind="tool_call",
        input_data=input_data,
        status=status,
        completed=completed,
        predicate=lambda invocation: (
            invocation.method == "web__run"
            and invocation.tool_name == "web_search"
            and normalize_web_correlation_value(query)
            in static_web_search_queries(invocation.input)
        ),
        output=output,
        turn_id=_as_non_empty_str(payload.get("turn_id")),
        started_at=started_at,
        completed_at=completed_at,
        provenance={"native_item_kind": "WebSearch", **timing},
    )

def handle_native_extension_web_search(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    """Normalize the historical Extension spelling of Codex web activity.

    Older Desktop JSONL persists web actions as an ``Extension`` item with
    ``kind=web.search`` rather than a ``WebSearch`` ThreadItem. Its
    terminal record is authoritative: the action, query, result cards, and
    timing all come from the provider event, not the JavaScript wrapper.
    """

    item = payload.get("item")
    if (
        not isinstance(item, dict)
        or item.get("type") != "Extension"
        or item.get("kind") != "web.search"
    ):
        return
    native_id = _as_non_empty_str(item.get("id"))
    if native_id is None:
        return
    action = item.get("action")
    action_data = action if isinstance(action, dict) else {}
    action_type = _as_non_empty_str(action_data.get("type"))
    query = _as_non_empty_str(item.get("query"))
    query_values = extension_web_correlation_values(query, action_data)
    input_data: dict[str, Any] = {
        "kind": "web.search",
        "action": action_data,
    }
    if query is not None:
        input_data["query"] = query
    output = {"results": item.get("results")} if "results" in item else None
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type="Extension:web.search",
        native_id=native_id,
        tool_name="web_search" if action_type == "search" else "web_fetch",
        item_kind="tool_call",
        input_data=input_data,
        # Extension/web.search has no per-item status field. A terminal
        # record carrying its result cards is Codex's direct completion
        # evidence, including when wrapper output later fails to render.
        status=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
        completed=completed,
        predicate=lambda invocation: static_web_matches_extension(
            invocation,
            action_type=action_type,
            query_values=query_values,
        ),
        output=output,
        turn_id=_as_non_empty_str(payload.get("turn_id")),
        started_at=started_at,
        completed_at=completed_at,
        provenance={
            "native_item_type": "Extension",
            "native_item_kind": "web.search",
            "native_action_type": action_type,
            **timing,
        },
    )

def handle_native_terminal_item(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
) -> None:
    """Project other completed Codex ThreadItems without wrapper guessing.

    This is intentionally terminal-only. These historical item spellings
    often have no ``item_started`` counterpart, and their completed record
    already contains the authoritative input, output, status, and timing.
    Message/reasoning/runtime item types retain their dedicated transcript
    paths below so this decoder does not manufacture duplicate content.
    """

    item = payload.get("item")
    if not isinstance(item, dict):
        return
    item_type = _as_non_empty_str(item.get("type"))
    native_id = _as_non_empty_str(item.get("id"))
    if item_type is None or native_id is None:
        return

    # These either have a dedicated handler above or are content/runtime
    # records rather than standalone user-visible actions.
    if item_type in {
        "AgentMessage",
        "CommandExecution",
        "ContextCompaction",
        "FileChange",
        "Plan",
        "Reasoning",
        "UserMessage",
        "WebSearch",
        "CollabAgentToolCall",
    }:
        return

    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    turn_id = _as_non_empty_str(payload.get("turn_id"))
    status = native_terminal_status(item, completed=True)
    input_data: dict[str, Any]
    output: Any = None
    tool_name: str
    native_type = item_type
    provenance: dict[str, Any] = {
        "native_item_type": item_type,
        **timing,
    }

    if item_type == "Extension":
        kind = _as_non_empty_str(item.get("kind"))
        if kind == "web.search":
            # Handled above with action-specific correlation semantics.
            return
        native_type = f"Extension:{kind or 'unknown'}"
        tool_name = f"extension.{kind}" if kind is not None else "extension"
        input_data = {
            key: item[key]
            for key in ("kind", "query", "action")
            if item.get(key) is not None
        }
        output = {
            key: item[key]
            for key in ("results", "result", "error")
            if item.get(key) is not None
        } or None
        provenance["native_item_kind"] = kind

    elif item_type == "McpToolCall":
        server = _as_non_empty_str(item.get("server"))
        tool = _as_non_empty_str(item.get("tool"))
        tool_name = (
            f"mcp__{server}__{tool}"
            if server is not None and tool is not None
            else "mcp_tool_call"
        )
        input_data = {
            key: item[key]
            for key in ("server", "tool", "arguments", "read_only_hint")
            if item.get(key) is not None
        }
        output = {
            key: item[key]
            for key in ("result", "error")
            if item.get(key) is not None
        } or None

    elif item_type == "DynamicToolCall":
        namespace = _as_non_empty_str(item.get("namespace"))
        tool = _as_non_empty_str(item.get("tool"))
        tool_name = (
            f"dynamic__{namespace}__{tool}"
            if namespace is not None and tool is not None
            else "dynamic_tool_call"
        )
        input_data = {
            key: item[key]
            for key in ("namespace", "tool", "arguments")
            if item.get(key) is not None
        }
        output = {
            key: item[key]
            for key in ("content_items", "error")
            if item.get(key) is not None
        } or None

    elif item_type == "ImageView":
        tool_name = "view_image"
        input_data = {"path": item["path"]} if item.get("path") is not None else {}

    elif item_type == "SubAgentActivity":
        kind = _as_non_empty_str(item.get("kind"))
        agent_thread_id = _as_non_empty_str(item.get("agent_thread_id"))
        tool_name = "collab_agent"
        input_data = {
            key: value
            for key, value in {
                "action": kind,
                "session": agent_thread_id,
                "agent_path": item.get("agent_path"),
            }.items()
            if value is not None
        }
        if kind == "started" and agent_thread_id is not None:
            state.spawn_links.setdefault(agent_thread_id, native_id)
        provenance["native_item_kind"] = kind

    else:
        # Preserve a future terminal action rather than silently dropping
        # it. Content/runtime variants above stay excluded deliberately.
        tool_name = f"codex_native.{item_type}"
        input_data = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "id",
                "status",
                "success",
                "result",
                "results",
                "error",
                "content_items",
            }
        }
        output = {
            key: item[key]
            for key in ("result", "results", "error", "content_items")
            if item.get(key) is not None
        } or None

    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type=native_type,
        native_id=native_id,
        tool_name=tool_name,
        item_kind="tool_call",
        input_data=input_data,
        status=status,
        completed=True,
        predicate=lambda _invocation: False,
        output=output,
        turn_id=turn_id,
        started_at=started_at,
        completed_at=completed_at,
        provenance=provenance,
    )

def handle_native_plan(
    payload: dict,
    timestamp: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    completed: bool,
) -> None:
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "Plan":
        return
    native_id = _as_non_empty_str(item.get("id"))
    text = _as_non_empty_str(item.get("text"))
    if native_id is None:
        return
    input_data = {"text": text} if text is not None else {}
    status = _tool_status(
        item.get("status"),
        default=ToolStatus.COMPLETED if completed else ToolStatus.IN_PROGRESS,
    )
    started_at, completed_at, timing = native_item_timing(payload, timestamp)
    record_native_activity(
        state=state,
        transcript=transcript,
        timestamp=timestamp,
        native_type="Plan",
        native_id=native_id,
        tool_name="update_plan",
        item_kind="plan",
        input_data=input_data,
        status=status,
        completed=completed,
        predicate=lambda invocation: invocation.method == "update_plan",
        turn_id=_as_non_empty_str(payload.get("turn_id")),
        started_at=started_at,
        completed_at=completed_at,
        provenance={"native_item_kind": "Plan", **timing},
    )
