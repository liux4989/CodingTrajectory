"""Route public methods to their durable control-plane authority.

The dispatcher is deliberately unaware of local discovery, HTTP, Supabase, or
cache implementation details. Embedded and remote runtimes provide the same
four handlers and therefore share request and response validation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol

from coding_trajectory.contracts import SERVICE_CONTRACTS, service_contract


class MethodAuthority(StrEnum):
    """Durable owner of one public service method."""

    HISTORICAL = "historical"
    PROJECT_INVENTORY = "project_inventory"
    LIVING = "living"
    ESTIMATION = "estimation"


AUTHORITY_METHODS: dict[MethodAuthority, frozenset[str]] = {
    MethodAuthority.HISTORICAL: frozenset(
        {
            "project.sessions",
            "session.overview",
            "session.summary",
            "session.search",
            "session.tree",
            "graph.overview",
            "session.stats",
            "graph.stats",
            "session.usage",
            "graph.usage",
            "session.model_usage",
            "session.request_usage",
            "session.tool_usage",
            "session.events",
            "session.items",
        }
    ),
    MethodAuthority.PROJECT_INVENTORY: frozenset({"project.list"}),
    MethodAuthority.LIVING: frozenset({"living.events", "living.sessions"}),
    MethodAuthority.ESTIMATION: frozenset(
        {
            "estimate.predict",
            "estimate.bind",
            "estimate.get",
            "estimate.list",
            "estimate.calibration",
            "estimate.backfill.start",
            "estimate.backfill.status",
        }
    ),
}

METHOD_AUTHORITIES = {
    method: authority
    for authority, methods in AUTHORITY_METHODS.items()
    for method in methods
}

_registered_methods = frozenset(SERVICE_CONTRACTS)
_owned_methods = frozenset(METHOD_AUTHORITIES)
if _registered_methods != _owned_methods:
    missing = sorted(_registered_methods - _owned_methods)
    unknown = sorted(_owned_methods - _registered_methods)
    raise RuntimeError(
        "control-plane authority map does not match service contracts: "
        f"missing={missing}, unknown={unknown}"
    )


def method_authority(method: str) -> MethodAuthority:
    """Return the declared authority for a registered service method."""

    service_contract(method)
    return METHOD_AUTHORITIES[method]


class AuthorityHandler(Protocol):
    """One authority's transport-independent application entry point."""

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        """Execute a request whose parameters have already been validated."""


class ApplicationDispatcher:
    """Validate and route all public CT methods through explicit authorities."""

    def __init__(
        self,
        handlers: Mapping[MethodAuthority, AuthorityHandler | Callable[..., Any]],
    ) -> None:
        missing = set(MethodAuthority) - set(handlers)
        if missing:
            names = ", ".join(sorted(authority.value for authority in missing))
            raise ValueError(f"missing control-plane authority handlers: {names}")
        self._handlers = dict(handlers)

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Validate one method call, route it, and validate its result."""

        contract = service_contract(method)
        validated_params = contract.validate_request(dict(params))
        handler = self._handlers[method_authority(method)]
        result = handler(method, validated_params)
        return contract.validate_response(result)
