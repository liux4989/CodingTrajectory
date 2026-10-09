"""Request-scoped Core process so abandoned local reads can be stopped."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coding_trajectory.runtime import ServiceRuntime


def main() -> None:
    request = json.load(sys.stdin)
    with ServiceRuntime(global_scope=True, current_dir=Path.cwd(), source="local") as core:
        result = core.execute(request)
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
