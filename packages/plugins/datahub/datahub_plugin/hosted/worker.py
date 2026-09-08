"""Cloudflare Python Worker entrypoint for the private Datahub facade."""

from __future__ import annotations

from uuid import UUID

from coding_trajectory.control_plane.remote import RemoteControlPlaneError
from workers import Response, WorkerEntrypoint

from datahub_plugin.hosted.service import (
    HostedDatahubService,
    HostedRequestError,
)
from datahub_plugin.hosted.transport import SupabaseAsyncRpcClient


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        response_headers = {
            "Cache-Control": "no-store",
            "Content-Type": "application/json; charset=utf-8",
            "X-Content-Type-Options": "nosniff",
        }
        method = str(request.method)
        if method != "GET":
            return _error(404, "not found", response_headers)
        authorization = request.headers.get("authorization") or ""
        prefix = "Bearer "
        access_token = (
            authorization[len(prefix) :].strip()
            if authorization.startswith(prefix)
            else ""
        )
        if not access_token or len(access_token) > 8192:
            return _error(401, "Supabase sign-in required", response_headers)
        try:
            workspace_id = UUID(str(self.env.CT_REMOTE_WORKSPACE_ID))
            supabase_url = str(self.env.CT_SUPABASE_URL)
            api_key = str(self.env.CT_SUPABASE_ANON_KEY)
        except (AttributeError, TypeError, ValueError):
            return _error(503, "hosted Datahub is not configured", response_headers)

        rpc = SupabaseAsyncRpcClient(
            url=supabase_url, api_key=api_key, access_token=access_token
        )
        try:
            service = HostedDatahubService(rpc=rpc, workspace_id=workspace_id)
            payload, status = await service.handle_url(
                method=method, raw_url=request.url
            )
            return Response.from_json(payload, status=status, headers=response_headers)
        except HostedRequestError as exc:
            return _error(exc.status, str(exc), response_headers)
        except RemoteControlPlaneError:
            return _error(
                502, "remote Datahub authority is unavailable", response_headers
            )
        except Exception:  # noqa: BLE001 - Worker boundary returns a sanitized error
            return _error(500, "unexpected hosted Datahub error", response_headers)
        finally:
            await rpc.close()


def _error(status: int, message: str, headers: dict[str, str]) -> Response:
    return Response.from_json(
        {"error": {"message": message}}, status=status, headers=headers
    )
