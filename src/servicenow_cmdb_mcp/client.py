"""Async ServiceNow REST client with OAuth 2.0 authentication, pagination, and retry."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.errors import (
    AuthError,
    InstanceError,
    SNTimeoutError,
    error_from_status,
)

logger = logging.getLogger(__name__)


def _parse_retry_after(value: str | None) -> int:
    """Parse a Retry-After header value to integer seconds.

    Only handles integer values. HTTP-date values are not parsed and
    return 0, falling back to the backoff calculation. Returns 0 on
    missing or unparseable values rather than crashing.
    """
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def resolve_ref(value: Any) -> str:
    """Extract sys_id from a ServiceNow reference field value.

    Reference fields may return as a plain sys_id string or as an object
    like {"link": "...", "value": "sys_id"}. This normalizes both formats.
    """
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return str(value) if value else ""


class ServiceNowClient:
    """Async HTTP client for ServiceNow REST APIs.

    Handles OAuth 2.0 password grant authentication with automatic token refresh,
    connection pooling, pagination, and retry with exponential backoff on transient errors.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = self._strip_credentials(settings.instance_url.rstrip("/"))
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.request_timeout),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

        # Token state
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @staticmethod
    def _strip_credentials(url: str) -> str:
        """Strip any embedded credentials (userinfo) from a URL."""
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            host_port = parsed.hostname or ""
            if parsed.port:
                host_port += f":{parsed.port}"
            parsed = parsed._replace(netloc=host_port)
        return urlunparse(parsed)

    @property
    def base_url(self) -> str:
        """Return the ServiceNow instance base URL (no trailing slash, no credentials)."""
        return self._base_url

    @property
    def username(self) -> str:
        """Return the authenticated ServiceNow username."""
        return self._settings.username

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ── OAuth 2.0 ────────────────────────────────────────────────────────

    async def _password_grant(self) -> None:
        """Obtain tokens via OAuth 2.0 password grant against /oauth_token.do."""
        logger.info("Authenticating via OAuth 2.0 password grant")
        response = await self._http.post(
            "/oauth_token.do",
            data={
                "grant_type": "password",
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "username": self._settings.username,
                "password": self._settings.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise AuthError("OAuth password grant failed. Check credentials and instance URL.")

        data = response.json()
        if "error" in data:
            # Only expose the error code, not the full description which may echo credentials
            error_code = data.get("error", "unknown_error")
            raise AuthError(f"OAuth error: {error_code}. Verify client_id, client_secret, username, and password.")

        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in", 1800))
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("OAuth tokens obtained, expires in %ds", expires_in)

    async def _refresh_grant(self) -> None:
        """Refresh the access token using the refresh token."""
        if not self._refresh_token:
            await self._password_grant()
            return

        logger.info("Refreshing OAuth token")
        response = await self._http.post(
            "/oauth_token.do",
            data={
                "grant_type": "refresh_token",
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "refresh_token": self._refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            logger.warning("Token refresh failed, falling back to password grant")
            await self._password_grant()
            return

        data = response.json()
        if "error" in data:
            logger.warning("Token refresh returned error, falling back to password grant")
            await self._password_grant()
            return

        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        expires_in = int(data.get("expires_in", 1800))
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("OAuth token refreshed, expires in %ds", expires_in)

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed.

        Refreshes 60 seconds before expiry to avoid mid-request expirations.
        Uses a lock to prevent concurrent token refresh races.
        """
        async with self._token_lock:
            if self._access_token is None:
                await self._password_grant()
            elif time.monotonic() >= self._token_expires_at - 60:
                await self._refresh_grant()
            assert self._access_token is not None  # noqa: S101
            return self._access_token

    def _invalidate_token(self) -> None:
        """Clear cached tokens to force re-authentication."""
        self._access_token = None
        self._refresh_token = None
        self._token_expires_at = 0.0

    # ── HTTP requests with retry ─────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated request to ServiceNow with retry logic.

        Retries on HTTP 429 and 503 with exponential backoff + jitter.
        On HTTP 401, invalidates the token cache, re-authenticates, and retries once.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            path: API path (e.g. /api/now/table/cmdb_ci).
            params: Query parameters.
            json_body: JSON request body for POST/PUT/PATCH.

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            ServiceNowError: On non-retryable errors after exhausting retries.
        """
        retries = 0
        retried_auth = False

        while True:
            token = await self._ensure_token()
            try:
                response = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TimeoutException:
                raise SNTimeoutError(f"Request to {path} timed out after {self._settings.request_timeout}s")
            except httpx.ConnectError as exc:
                raise InstanceError(f"Connection to {self._base_url} failed: {exc}") from exc
            except httpx.NetworkError as exc:
                raise InstanceError(f"Network error on {path}: {exc}") from exc

            # Success
            if response.status_code in (200, 201, 204):
                if response.status_code == 204:
                    return {}
                return response.json()

            # 401 — re-authenticate once
            if response.status_code == 401 and not retried_auth:
                logger.warning("HTTP 401 — invalidating token and re-authenticating")
                self._invalidate_token()
                retried_auth = True
                continue

            # Retryable errors: 429, 503
            if response.status_code in (429, 503) and retries < self._settings.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                backoff = min(2**retries + random.uniform(0, 1), 30)  # noqa: S311
                wait = max(retry_after, backoff)
                logger.warning(
                    "HTTP %d on %s — retry %d/%d in %.1fs",
                    response.status_code,
                    path,
                    retries + 1,
                    self._settings.max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                retries += 1
                continue

            # Non-retryable or exhausted retries — raise structured error
            error_msg = self._extract_error_message(response)
            retry_after = (
                _parse_retry_after(response.headers.get("Retry-After"))
                if response.status_code == 429
                else None
            )
            error = error_from_status(response.status_code, error_msg, retry_after)

            # Log permission denials for audit trail
            if response.status_code == 403:
                logger.warning("PERMISSION_DENIED path=%s", path)

            raise error

    @staticmethod
    def _extract_error_message(response: httpx.Response) -> str:
        """Pull a human-readable message from a ServiceNow error response.

        Avoids leaking internal details by capping message length and
        falling back to a generic message rather than dumping the full body.
        """
        try:
            body = response.json()
            err = body.get("error", {})
            if isinstance(err, dict):
                msg = err.get("message", "") or err.get("detail", "")
                if msg:
                    return msg[:500]
                return f"HTTP {response.status_code} error (no detail provided)"
            return str(err)[:500]
        except Exception:
            return f"HTTP {response.status_code} error"

    # ── Convenience methods ──────────────────────────────────────────────

    async def get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Execute an authenticated GET request."""
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, json_body: dict[str, Any], params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Execute an authenticated POST request."""
        return await self.request("POST", path, params=params, json_body=json_body)

    async def patch(
        self, path: str, json_body: dict[str, Any], params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Execute an authenticated PATCH request."""
        return await self.request("PATCH", path, params=params, json_body=json_body)

    async def delete(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Execute an authenticated DELETE request."""
        return await self.request("DELETE", path, params=params)

    # ── Pagination helper ────────────────────────────────────────────────

    async def get_records(
        self,
        table: str,
        query: str = "",
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order_by: str = "ORDERBYsys_created_on",
    ) -> list[dict[str, Any]]:
        """Fetch records from a ServiceNow table with pagination.

        Always uses sysparm_fields and stable ordering per CLAUDE.md conventions.

        Args:
            table: ServiceNow table name (e.g. cmdb_ci_server).
            query: Encoded query string.
            fields: List of field names to return. Required — never fetch full records.
            limit: Max records to return. Defaults to settings.default_limit.
            offset: Pagination offset.
            order_by: Order clause appended to query. Defaults to ORDERBYsys_created_on.

        Returns:
            List of record dicts with only the requested fields.
        """
        effective_limit = min(limit or self._settings.default_limit, self._settings.max_limit)

        full_query = query
        if order_by:
            full_query = f"{full_query}^{order_by}" if full_query else order_by

        params: dict[str, str] = {
            "sysparm_query": full_query,
            "sysparm_limit": str(effective_limit),
            "sysparm_offset": str(offset),
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)

        response = await self.get(f"/api/now/table/{table}", params=params)
        return response.get("result", [])

    async def get_record(
        self,
        table: str,
        sys_id: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single record by sys_id.

        Args:
            table: ServiceNow table name.
            sys_id: The record's sys_id.
            fields: List of field names to return.

        Returns:
            Record dict, or None if the response contains no result body.

        Raises:
            NotFoundError: If the sys_id does not exist (HTTP 404).
        """
        params: dict[str, str] = {}
        if fields:
            params["sysparm_fields"] = ",".join(fields)

        response = await self.get(f"/api/now/table/{table}/{sys_id}", params=params)
        return response.get("result")

    async def get_aggregate(
        self,
        table: str,
        query: str = "",
        group_by: str = "",
    ) -> dict[str, Any]:
        """Get aggregate counts using the Stats API.

        Uses /api/now/stats/{table} instead of fetching records per CLAUDE.md.

        Args:
            table: ServiceNow table name.
            query: Encoded query string.
            group_by: Field to group results by.

        Returns:
            Aggregate API response.
        """
        params: dict[str, str] = {"sysparm_count": "true"}
        if query:
            params["sysparm_query"] = query
        if group_by:
            params["sysparm_group_by"] = group_by

        return await self.get(f"/api/now/stats/{table}", params=params)
