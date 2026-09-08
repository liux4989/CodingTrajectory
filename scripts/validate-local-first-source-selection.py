"""Validate local-first selection; optionally smoke-test read-only remote fallback."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from coding_trajectory.control_plane import MethodAuthority
from coding_trajectory.control_plane.remote import RemoteControlPlaneError
from coding_trajectory.query import ResourceNotFoundError
from coding_trajectory.runtime import LocalSourceUnavailableError, ServiceRuntime


class StubHistoricalRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def pin_snapshot(self) -> int:
        return 0

    def store_for(self, method: str, params: dict[str, Any]) -> tuple[Any, str]:
        if self.error is not None:
            raise self.error
        raise AssertionError(f"unexpected historical call: {method} {params}")

    def metadata(self) -> dict[str, Any]:
        return {"source": "local", "freshness": "live"}


class StubRemoteRuntime:
    def __init__(
        self, *, method: str, params: dict[str, Any], result: dict[str, Any]
    ) -> None:
        self.method = method
        self.params = params
        self.result = result
        self.calls = 0

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == self.method
        assert params == self.params
        self.calls += 1
        return self.result

    def transport_metadata(self) -> dict[str, Any]:
        return {
            "source": "remote",
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "snapshot_sequence": 7,
        }

    def close(self) -> None:
        return


def runtime_with_inventory(inventory: Any, fallback_factory: Any) -> ServiceRuntime:
    def local_inventory(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "project.list"
        assert params == {}
        if isinstance(inventory, Exception):
            raise inventory
        return inventory

    return ServiceRuntime(
        global_scope=True,
        current_dir=Path.cwd(),
        historical_repository=StubHistoricalRepository(),
        authority_handlers={MethodAuthority.PROJECT_INVENTORY: local_inventory},
        fallback_factory=fallback_factory,
    )


def runtime_with_historical(error: Exception, fallback_factory: Any) -> ServiceRuntime:
    return ServiceRuntime(
        global_scope=True,
        current_dir=Path.cwd(),
        historical_repository=StubHistoricalRepository(error),
        fallback_factory=fallback_factory,
    )


def validate_local_preference_and_empty_success() -> None:
    fallback_calls = 0

    def forbidden_fallback() -> StubRemoteRuntime:
        nonlocal fallback_calls
        fallback_calls += 1
        raise AssertionError("remote fallback must remain lazy")

    for result in (
        {"items": {"CodingTrajectory": {"path": None, "vendors": ["codex_cli"]}}},
        {"items": {}},
    ):
        with runtime_with_inventory(result, forbidden_fallback) as runtime:
            response = runtime.execute({"method": "project.list", "params": {}})
        assert response["ok"] is True
        assert set(response["result"]["items"]) == set(result["items"])
        assert response["meta"]["source"] == "local"
    assert fallback_calls == 0


def validate_explicit_fallback_conditions() -> None:
    session_id = "00000000-0000-0000-0000-000000000002"
    remote = StubRemoteRuntime(
        method="session.overview",
        params={"session_id": session_id},
        result={"root_session_id": session_id, "sessions": []},
    )
    with runtime_with_historical(
        ResourceNotFoundError("session not found locally"), lambda: remote
    ) as runtime:
        response = runtime.execute(
            {"method": "session.overview", "params": {"session_id": session_id}}
        )
    assert response["ok"] is True
    assert response["result"]["root_session_id"] == session_id
    assert response["meta"]["source"] == "remote"
    assert remote.calls == 1

    remote = StubRemoteRuntime(
        method="project.list",
        params={},
        result={"items": {"RemoteProject": {"path": None, "vendors": ["codex_cli"]}}},
    )
    with runtime_with_inventory(
        LocalSourceUnavailableError("local discovery unavailable"), lambda: remote
    ) as runtime:
        response = runtime.execute({"method": "project.list", "params": {}})
    assert response["ok"] is True
    assert "RemoteProject" in response["result"]["items"]
    assert response["meta"]["source"] == "remote"
    assert remote.calls == 1


def validate_remote_unavailable_and_legitimate_errors() -> None:
    def unavailable() -> StubRemoteRuntime:
        raise RemoteControlPlaneError("offline")

    with runtime_with_historical(
        ResourceNotFoundError("record absent locally"), unavailable
    ) as runtime:
        response = runtime.execute(
            {
                "method": "session.overview",
                "params": {"session_id": "00000000-0000-0000-0000-000000000002"},
            }
        )
    assert response["ok"] is False
    assert "record absent locally" in response["error"]["message"]
    assert "remote Chronicles fallback failed (offline)" in response["error"]["message"]

    fallback_calls = 0

    def forbidden_fallback() -> StubRemoteRuntime:
        nonlocal fallback_calls
        fallback_calls += 1
        raise AssertionError("validation errors must not fall through")

    with runtime_with_inventory(
        ValueError("invalid local request"), forbidden_fallback
    ) as runtime:
        response = runtime.execute({"method": "project.list", "params": {}})
    assert response["ok"] is False
    assert response["error"]["message"] == "invalid local request"
    assert fallback_calls == 0


def validate_read_only_remote_fallback(profile_name: str) -> None:
    """Use an existing credential profile for one inventory-only fallback."""

    from coding_trajectory.control_plane.http_service import RemoteRuntimeFactory
    from coding_trajectory_cli.collector_credentials import refresh_profile

    credentials = refresh_profile(profile_name)
    factory = RemoteRuntimeFactory(
        url=str(credentials.profile.supabase_url),
        api_key=credentials.profile.supabase_api_key,
        workspace_id=credentials.profile.workspace_id,
    )

    def remote_runtime() -> ServiceRuntime:
        return factory.build(
            credentials.access_token,
            local_evidence=False,
            current_dir=Path.cwd(),
        )

    with runtime_with_inventory(
        LocalSourceUnavailableError("forced unavailable for remote smoke"),
        remote_runtime,
    ) as runtime:
        response = runtime.execute({"method": "project.list", "params": {}})
    assert response["ok"] is True, response.get("error", {}).get("message")
    assert response["meta"]["source"] == "remote"
    print(
        "read-only remote fallback passed "
        f"({len(response['result']['items'])} projects, "
        f"snapshot {response['meta']['snapshot_sequence']})"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--remote-profile",
        help="Also run one read-only project inventory fallback with this profile.",
    )
    args = parser.parse_args()
    validate_local_preference_and_empty_success()
    validate_explicit_fallback_conditions()
    validate_remote_unavailable_and_legitimate_errors()
    print("local-first source selection validation passed")
    if args.remote_profile:
        validate_read_only_remote_fallback(args.remote_profile)


if __name__ == "__main__":
    main()
