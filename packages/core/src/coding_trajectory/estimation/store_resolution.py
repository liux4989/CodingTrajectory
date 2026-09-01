"""DocumentStore resolution for estimation handlers.

Imports of ``coding_trajectory.service`` stay function-local: the service
layer dispatches into estimation, so a module-level edge would create a
cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def store_for_turn(
    turn_id: str,
    *,
    global_scope: bool,
    current_dir: Path,
    cache: Any,
) -> Any:
    from coding_trajectory.service import resolve_store

    store, _ = resolve_store(
        {"turn_id": turn_id},
        global_scope=global_scope,
        current_dir=current_dir,
        cache=cache,
        include_descendants=True,
    )
    return store


def full_store(params: dict[str, Any], *, current_dir: Path, cache: Any) -> Any:
    from coding_trajectory.service import resolve_store

    store, _ = resolve_store(
        {
            key: params[key]
            for key in ("project_name", "since_days", "agent_vendor")
            if params.get(key) is not None
        },
        global_scope=True,
        current_dir=current_dir,
        cache=cache,
    )
    return store
