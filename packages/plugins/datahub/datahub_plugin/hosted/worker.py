"""Cloudflare Python Worker entrypoint for the private Datahub facade."""

from __future__ import annotations

from workers import Response, WorkerEntrypoint


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
        try:
            from uuid import UUID

            from coding_trajectory.control_plane.remote import RemoteControlPlaneError
            from service import HostedDatahubService, HostedRequestError
            from transport import SupabaseAsyncRpcClient
        except Exception:  # noqa: BLE001 - fail closed on runtime import failure
            return _error(
                503, "hosted Datahub runtime is unavailable", response_headers
            )
        try:
            workspace_id = UUID(str(self.env.CT_REMOTE_WORKSPACE_ID))
            supabase_url = str(self.env.CT_SUPABASE_URL)
            api_key = str(self.env.CT_SUPABASE_ANON_KEY)
            reader_email = str(self.env.CT_READER_EMAIL)
            reader_password = str(self.env.CT_READER_PASSWORD)
        except (AttributeError, TypeError, ValueError):
            return _error(503, "hosted Datahub is not configured", response_headers)

        if not all((supabase_url, api_key, reader_email, reader_password)):
            return _error(503, "hosted Datahub is not configured", response_headers)

        rpc: SupabaseAsyncRpcClient | None = None
        try:
            rpc = SupabaseAsyncRpcClient(
                url=supabase_url,
                api_key=api_key,
                reader_email=reader_email,
                reader_password=reader_password,
            )
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
            if rpc is not None:
                try:
                    await rpc.close()
                except Exception:  # noqa: BLE001, S110 - cleanup must not replace response
                    pass


def _error(status: int, message: str, headers: dict[str, str]) -> Response:
    return Response.from_json(
        {"error": {"message": message}}, status=status, headers=headers
    )
