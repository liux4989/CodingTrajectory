"""Supabase-backed authority handler for durable living observations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.contracts import service_contract
from coding_trajectory.control_plane.remote import (
    RemoteControlPlaneError,
    SupabaseRpcClient,
)

_LIVING_METHODS = frozenset({"living.events", "living.sessions"})


class _RemoteLivingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    result: dict[str, Any]


class _RemoteLivingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    snapshot_sequence: int = Field(ge=0)
    evaluated_at: datetime
    results: list[_RemoteLivingResult]


class SupabaseLivingAuthority:
    """Validate and serve living methods from one remote workspace authority.

    ``call_batch`` sends every call in one RPC so PostgreSQL selects one
    workspace sequence and one lease-freshness instant for the whole batch.
    An optional ``snapshot_sequence`` supports an explicitly pinned reader.
    """

    def __init__(
        self,
        *,
        client: SupabaseRpcClient,
        workspace_id: UUID,
        snapshot_sequence: int | None = None,
    ) -> None:
        if snapshot_sequence is not None and snapshot_sequence < 0:
            raise ValueError("snapshot_sequence must not be negative")
        self._client = client
        self.workspace_id = workspace_id
        self._pinned_sequence = snapshot_sequence
        self.snapshot_sequence = snapshot_sequence
        self.evaluated_at: datetime | None = None

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Implement the control-plane ``AuthorityHandler`` protocol."""

        return self.call(method, params)

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Serve and validate one living method."""

        return self.call_batch([(method, params)])[0]

    def call_batch(
        self,
        calls: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Serve a non-empty batch pinned to one workspace sequence."""

        if not calls:
            raise ValueError("remote living batch must not be empty")
        validated_calls: list[dict[str, Any]] = []
        contracts = []
        for method, params in calls:
            if method not in _LIVING_METHODS:
                raise KeyError(f"not a living authority method: {method}")
            contract = service_contract(method)
            contracts.append(contract)
            validated_calls.append(
                {
                    "method": method,
                    "params": contract.validate_request(dict(params)),
                }
            )

        request: dict[str, Any] = {
            "workspace_id": str(self.workspace_id),
            "calls": validated_calls,
        }
        if self._pinned_sequence is not None:
            request["snapshot_sequence"] = self._pinned_sequence
        response = _RemoteLivingResponse.model_validate(
            self._client.call("ct_remote_living", request)
        )
        if response.workspace_id != self.workspace_id:
            raise RemoteControlPlaneError("remote living workspace mismatch")
        if len(response.results) != len(validated_calls):
            raise RemoteControlPlaneError("remote living result count mismatch")

        results: list[dict[str, Any]] = []
        for expected, contract, returned in zip(
            validated_calls, contracts, response.results, strict=True
        ):
            if returned.method != expected["method"]:
                raise RemoteControlPlaneError("remote living result order mismatch")
            results.append(contract.validate_response(returned.result))
        self.snapshot_sequence = response.snapshot_sequence
        self.evaluated_at = response.evaluated_at
        return results

    def metadata(self) -> dict[str, Any] | None:
        """Return authority facts after the first successful call."""

        if self.snapshot_sequence is None:
            return None
        return {
            "workspace_id": str(self.workspace_id),
            "snapshot_sequence": self.snapshot_sequence,
            "source": "remote",
            "freshness": "lease_expiry",
            "content_scope": "durable_living_observations",
            **(
                {"evaluated_at": self.evaluated_at.isoformat()}
                if self.evaluated_at is not None
                else {}
            ),
        }


__all__ = ["SupabaseLivingAuthority"]
