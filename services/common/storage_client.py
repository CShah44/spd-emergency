"""The ONLY way app services talk to storage. DESIGN.md section 8.

Owned by Person A. Person B calls it and asks A for any change.

    from common.storage_client import StorageClient, VersionConflict

    store = StorageClient()                      # uses COORDINATOR_URL
    saved = store.put("resource:res-1", record)  # saved["version"] == 1
    try:
        store.put_if_version("resource:res-1", updated, version=saved["version"])
    except VersionConflict:
        ...  # someone else changed it first: re-read or try the next candidate

HTTP status codes become exceptions, so callers never inspect raw responses:
    404 -> NotFound          409 -> VersionConflict
    503, timeout, connection refused -> StorageUnavailable
    anything else unexpected -> StorageError (the base class of all of these)
"""

from typing import Any
from urllib.parse import quote

import httpx

from common import config


class StorageError(Exception):
    """Base class for every storage failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NotFound(StorageError):
    pass


class VersionConflict(StorageError):
    pass


class StorageUnavailable(StorageError):
    pass


class StorageClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ):
        """`client` lets tests inject e.g. FastAPI's TestClient instead of real HTTP."""
        if client is not None:
            self._http = client
        else:
            self._http = httpx.Client(
                base_url=base_url or config.COORDINATOR_URL,
                timeout=timeout if timeout is not None else config.STORAGE_TIMEOUT_S,
            )

    # -- context manager, so `with StorageClient() as s:` closes the connection pool
    def __enter__(self) -> "StorageClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- public API ---------------------------------------------------------

    def put(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Create or overwrite. Returns the saved record with its new version."""
        return self._send("PUT", _path(key), key, json=record).json()

    def put_if_version(self, key: str, record: dict[str, Any], version: int) -> dict[str, Any]:
        """Save only if the stored version is still `version` (compare-and-set).

        Raises VersionConflict if someone else saved first, NotFound if the key
        no longer exists.
        """
        # If-Match carries the version we read; the server refuses the save if it moved on.
        resp = self._send("PUT", _path(key), key, json=record, headers={"If-Match": str(version)})
        return resp.json()

    def get(self, key: str) -> dict[str, Any]:
        """Fetch one record. Raises NotFound."""
        return self._send("GET", _path(key), key).json()

    def get_or_none(self, key: str) -> dict[str, Any] | None:
        try:
            return self.get(key)
        except NotFound:
            return None

    def delete(self, key: str) -> None:
        """Delete one record. Raises NotFound."""
        self._send("DELETE", _path(key), key)

    def query(self, prefix: str = "", **filters: Any) -> list[dict[str, Any]]:
        """Search all shards. Filters are exact matches on top-level fields:
            store.query("resource:", type="water", status="AVAILABLE")
        Result order is not guaranteed; sort it yourself.
        """
        params = {"prefix": prefix, **{k: str(v) for k, v in filters.items()}}
        return self._send("GET", "/store", prefix, params=params).json()

    def status(self) -> dict[str, Any]:
        """Coordinator's view of every storage node (for demos)."""
        return self._send("GET", "/internal/status", "status").json()

    # -- internals -------------------------------------------------------------

    def _send(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.TransportError as exc:  # refused, DNS failure, timeout...
            raise StorageUnavailable(f"coordinator unreachable: {exc!r}") from exc

        if resp.is_success:
            return resp
        # Turn status codes into exceptions so callers write
        # `except VersionConflict:` instead of checking numbers.
        detail = _detail(resp)
        if resp.status_code == 404:
            raise NotFound(f"{what}: not found", 404)
        if resp.status_code == 409:
            raise VersionConflict(f"{what}: version conflict {detail}", 409)
        if resp.status_code == 503:
            raise StorageUnavailable(f"{what}: no live storage node {detail}", 503)
        raise StorageError(f"{method} {url} -> {resp.status_code} {detail}", resp.status_code)


def _path(key: str) -> str:
    # Keys look like "request:req-abc". Escape anything odd, keep ':' readable.
    return "/store/" + quote(key, safe=":")


def _detail(resp: httpx.Response) -> Any:
    try:
        return resp.json().get("detail")
    except Exception:
        return resp.text
