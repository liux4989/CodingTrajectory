"""Print remote environment variables, including secrets, from local profiles.

Run from the repository root:
    PYTHONPATH=packages/core/src uv run --no-sync python scripts/print-remote-env.py

Output contains bearer credentials. Do not paste it into chat or commit it.
"""

from __future__ import annotations

import shlex
import sys

from coding_trajectory.control_plane.connections import load_profile_credentials


def main() -> None:
    try:
        collector = load_profile_credentials("production-collector")
        reader = load_profile_credentials("production-reader")
    except Exception:  # noqa: BLE001 - avoid exposing credential-backend error details
        print(
            "Could not load the required profiles or their credentials.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    if (
        collector.profile.workspace_id != reader.profile.workspace_id
        or collector.profile.cloudflare_url != reader.profile.cloudflare_url
    ):
        print(
            "Reader and collector profiles target different destinations.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not reader.profile.cloudflare_url or not reader.profile.workspace_id:
        print("The remote URL or workspace ID is missing.", file=sys.stderr)
        raise SystemExit(1)
    if not collector.profile.agent_id:
        print("The collector agent ID is missing.", file=sys.stderr)
        raise SystemExit(1)

    values = {
        "CT_COLLECTOR_ACCESS_TOKEN": collector.access_token,
        "CT_ACCESS_TOKEN": reader.access_token,
        "CT_CLOUDFLARE_URL": str(reader.profile.cloudflare_url).rstrip("/"),
        "CT_REMOTE_WORKSPACE_ID": str(reader.profile.workspace_id),
        "CT_COLLECTOR_AGENT_ID": str(collector.profile.agent_id),
    }
    for name, value in values.items():
        print(f"export {name}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
