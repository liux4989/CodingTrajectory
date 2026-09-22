"""Authenticate and stream requests to the workspace HTTP authority.

Keep body parsing, hashing, and prepared responses out of the Free plan's
10 ms stateless CPU budget. The workspace retains all existing validation.
"""

from typing import Any
from urllib.parse import urlsplit

from http_handler import authenticate, error_response
from shared import MAX_BODY, MAX_BODY_BYTES
from workers import Response, WorkerEntrypoint
from workspace import Workspace


class Default(WorkerEntrypoint):
    async def fetch(self, request: Any) -> Response:
        try:
            principal = await authenticate(request, self.env)
            workspace = self.env.WORKSPACES.getByName(principal["workspace_id"])
            return await workspace.fetch(request)
        except Exception as error:  # noqa: BLE001 -- sanitize the forwarding boundary
            protocol = (
                "ct.api.v1" if urlsplit(request.url).path == "/v1/api" else "ct.core.v1"
            )
            return error_response(error, self.env, protocol=protocol)


__all__ = ["MAX_BODY", "MAX_BODY_BYTES", "Default", "Workspace"]
