"""Bounded cross-process coordination for one collector agent's publications."""

from __future__ import annotations

import fcntl
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID


@contextmanager
def publication_lock(workspace_id: UUID, agent_id: UUID, *, timeout: float = 120):
    key = hashlib.sha256(f"{workspace_id}:{agent_id}".encode()).hexdigest()
    path = Path("~/.coding-trajectory/control-plane/locks").expanduser() / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "another publication is still running; retry this query"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
