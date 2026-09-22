"""Validate the batch-release marker and detect one explicit release transition."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILE = "RELEASE.md"
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class ReleaseMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["ct.release.v1"] = Field(alias="schema")
    release_id: int = Field(ge=0)
    target: Literal["staging", "production"]
    title: str = Field(min_length=1, max_length=120)


def parse_marker(body: str) -> ReleaseMarker:
    lines = body.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("RELEASE.md must begin with strict front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("RELEASE.md front matter is not closed") from exc
    values: dict[str, object] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line or line.lstrip().startswith("#") or ":" not in line:
            raise ValueError(f"invalid RELEASE.md front matter line {number}")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or key in values or not value:
            raise ValueError(f"invalid RELEASE.md front matter line {number}")
        values[key] = int(value) if key == "release_id" else value
    return ReleaseMarker.model_validate(values)


def checked_ref(ref: str) -> str:
    if not REF.fullmatch(ref) or ".." in ref or ref.endswith("/"):
        raise ValueError("git refs must be simple names or full commit SHAs")
    return ref


def marker_at(ref: str) -> ReleaseMarker | None:
    ref = checked_ref(ref)
    if ref == "0" * 40:
        return None
    result = subprocess.run(
        ["git", "show", f"{ref}:{RELEASE_FILE}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if "does not exist" in result.stderr or "exists on disk" in result.stderr:
            return None
        raise ValueError(f"cannot read {RELEASE_FILE} at {ref}")
    return parse_marker(result.stdout)


def marker_in_checkout() -> ReleaseMarker:
    return parse_marker((ROOT / RELEASE_FILE).read_text())


def transition(base: ReleaseMarker | None, head: ReleaseMarker) -> dict[str, object]:
    if base is None:
        if head.release_id != 0:
            raise ValueError("the first release marker must establish release_id 0")
        ready = False
        reason = "baseline_created"
    else:
        increment = head.release_id - base.release_id
        if increment < 0:
            raise ValueError("release_id cannot decrease")
        if increment > 1:
            raise ValueError("release_id must advance by exactly one")
        if increment == 0 and head.target != base.target:
            raise ValueError("target changes require a release_id increment")
        ready = increment == 1
        reason = "release_id_incremented" if ready else "release_id_unchanged"
    return {
        "release_ready": ready,
        "release_id": head.release_id,
        "target": head.target,
        "title": head.title,
        "reason": reason,
    }


def write_github_output(path: Path, result: dict[str, object]) -> None:
    with path.open("a") as stream:
        for key in ("release_ready", "release_id", "target"):
            value = result[key]
            if isinstance(value, bool):
                value = str(value).lower()
            stream.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--ref")
    validate.add_argument("--expect-id", type=int)
    validate.add_argument("--expect-target", choices=("staging", "production"))
    change = subparsers.add_parser("change")
    change.add_argument("--base-ref", required=True)
    change.add_argument("--head-ref", required=True)
    change.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    if args.command == "validate":
        marker = marker_at(args.ref) if args.ref else marker_in_checkout()
        if marker is None:
            raise ValueError(f"{RELEASE_FILE} is missing")
        if args.expect_id is not None and marker.release_id != args.expect_id:
            raise ValueError("release_id does not match the requested batch")
        if args.expect_target is not None and marker.target != args.expect_target:
            raise ValueError("release target does not match the requested batch")
        result = marker.model_dump(by_alias=True)
    else:
        base = marker_at(args.base_ref)
        head = marker_at(args.head_ref)
        if head is None:
            raise ValueError(f"{RELEASE_FILE} is missing from the candidate")
        result = transition(base, head)
        if args.github_output:
            write_github_output(args.github_output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"release marker rejected: {exc}") from exc
