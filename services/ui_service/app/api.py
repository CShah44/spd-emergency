"""The page's connection to the request service: the same REST API the CLI uses,
plus GET /requests and /cluster. Every failure becomes an ApiError whose message
is fit to show to the person at the screen."""

import os
from typing import Any

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8080")


class ApiError(Exception):
    pass


def _message(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):  # a 400 listing every invalid field
        detail = "; ".join(f"{d.get('field') or 'request'}: {d.get('message')}" for d in detail)
    return f"{response.status_code}: {detail or response.reason_phrase}"


class DeltaApi:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._http = client or httpx.AsyncClient(base_url=base_url or API_URL, timeout=10.0)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"cannot reach Delta Command ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ApiError(_message(response))
        return response.json()

    async def close(self) -> None:
        await self._http.aclose()

    # -- reading ------------------------------------------------------------------------------------

    async def requests(self, limit: int = 100) -> list[dict]:
        """Active requests first, then finished ones (see GET /requests?active_first)."""
        return await self._call("GET", "/requests", params={"limit": limit, "active_first": "true"})

    async def request_summary(self) -> dict[str, int]:
        return await self._call("GET", "/requests/summary")

    async def resources(self) -> list[dict]:
        return await self._call("GET", "/resources")

    async def cluster(self) -> dict:
        return await self._call("GET", "/cluster")

    # -- acting ---------------------------------------------------------------------------------------

    async def create_request(self, body: dict) -> dict:
        return await self._call("POST", "/requests", json=body)

    async def register_resource(self, body: dict) -> dict:
        return await self._call("POST", "/resources", json=body)

    async def set_request_status(self, request_id: str, status: str) -> dict:
        return await self._call("PUT", f"/requests/{request_id}/status", json={"status": status})

    async def set_resource_status(self, resource_id: str, status: str) -> dict:
        return await self._call("PUT", f"/resources/{resource_id}/status", json={"status": status})

    async def match(self, request_id: str) -> dict:
        return await self._call("POST", f"/match/{request_id}")

    async def set_node_paused(self, node_id: str, paused: bool) -> dict:
        return await self._call("POST", f"/cluster/nodes/{node_id}/{'pause' if paused else 'resume'}")
