from __future__ import annotations

import argparse
import sys


def project(args: argparse.Namespace) -> int:
    import datahub_plugin.maintenance.cleanup as cleanup_mod

    try:
        payload = cleanup_mod.handle_project(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(cleanup_mod.render(args, payload))
    return 1 if (payload.get("summary") or {}).get("error_count") else 0


def session(args: argparse.Namespace) -> int:
    import datahub_plugin.maintenance.cleanup as cleanup_mod

    try:
        payload = cleanup_mod.handle_session(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(cleanup_mod.render(args, payload))
    return 1 if (payload.get("summary") or {}).get("error_count") else 0
