"""Tests for server.py — check_connection health-check tool and client utilities."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from servicenow_cmdb_mcp.client import ServiceNowClient


# ── Helpers ─────────────────────────────────────────────────────────


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings():
    """Minimal Settings-like object for check_connection."""
    settings = type("Settings", (), {
        "instance_url": "https://test.service-now.com",
        "client_id": "id",
        "client_secret": "secret",
        "username": "admin",
        "password": "pw",
        "cache_ttl": 3600,
    })()
    return settings


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.base_url = "https://test.service-now.com"
    client.get = AsyncMock(return_value={"result": [
        {
            "user_name": "admin",
            "sys_id": "abc123",
            "roles": "admin,itil,cmdb_read",
        }
    ]})
    return client


@pytest.fixture
def app_with_client(mock_settings, mock_client):
    """Create app with mocked settings and client."""
    with patch("servicenow_cmdb_mcp.server.Settings", return_value=mock_settings), \
         patch("servicenow_cmdb_mcp.server.ServiceNowClient", return_value=mock_client):
        from servicenow_cmdb_mcp.server import create_app
        mcp = create_app()

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map, mock_client


@pytest.fixture
def app_no_credentials():
    """Create app with no credentials (client=None)."""
    with patch("servicenow_cmdb_mcp.server.Settings", side_effect=Exception("No creds")):
        from servicenow_cmdb_mcp.server import create_app
        mcp = create_app()

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Tests ───────────────────────────────────────────────────────────


class TestCheckConnection:
    @pytest.mark.asyncio
    async def test_connected_with_user(self, app_with_client):
        tools, mock_client = app_with_client
        result = _parse(await tools["check_connection"]())
        assert result["connected"] is True
        assert result["instance_url"] == "https://test.service-now.com"
        assert result["authenticated_as"] == "admin"
        assert result["user_sys_id"] == "abc123"
        assert "admin" in result["roles"]
        assert "itil" in result["roles"]
        assert "suggested_next" in result

    @pytest.mark.asyncio
    async def test_connected_user_not_found(self, app_with_client):
        tools, mock_client = app_with_client
        mock_client.get.return_value = {"result": []}
        result = _parse(await tools["check_connection"]())
        assert result["connected"] is True
        assert result["roles"] == []
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_no_credentials(self, app_no_credentials):
        tools = app_no_credentials
        result = _parse(await tools["check_connection"]())
        assert result["connected"] is False
        assert result["error"] is True
        assert result["category"] == "AuthError"

    @pytest.mark.asyncio
    async def test_service_now_error(self, app_with_client):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        tools, mock_client = app_with_client
        mock_client.get.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["check_connection"]())
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_empty_roles_string(self, app_with_client):
        tools, mock_client = app_with_client
        mock_client.get.return_value = {"result": [
            {"user_name": "admin", "sys_id": "abc123", "roles": ""}
        ]}
        result = _parse(await tools["check_connection"]())
        assert result["connected"] is True
        assert result["roles"] == []


# ── URL credential sanitization ───────────────────────────────────


class TestStripCredentials:
    def test_no_credentials(self):
        assert ServiceNowClient._strip_credentials("https://instance.service-now.com") == "https://instance.service-now.com"

    def test_strips_username_password(self):
        result = ServiceNowClient._strip_credentials("https://admin:secret@instance.service-now.com")
        assert result == "https://instance.service-now.com"
        assert "admin" not in result
        assert "secret" not in result

    def test_strips_username_only(self):
        result = ServiceNowClient._strip_credentials("https://admin@instance.service-now.com")
        assert result == "https://instance.service-now.com"
        assert "admin" not in result

    def test_preserves_port(self):
        result = ServiceNowClient._strip_credentials("https://admin:pw@instance.service-now.com:8443")
        assert result == "https://instance.service-now.com:8443"
        assert "admin" not in result

    def test_preserves_path(self):
        result = ServiceNowClient._strip_credentials("https://admin:pw@instance.service-now.com/api/now")
        assert result == "https://instance.service-now.com/api/now"
