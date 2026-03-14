"""Shared test fixtures and mock client."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings with dummy credentials."""
    return Settings(
        instance_url="https://test.service-now.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        username="test-user",
        password="test-pass",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """Mock ServiceNowClient for tool-level tests."""
    client = AsyncMock()
    client.get = AsyncMock(return_value={"result": []})
    client.post = AsyncMock(return_value={"result": {}})
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value={})
    client.get_aggregate = AsyncMock(return_value={"result": {"stats": {"count": "0"}}})
    return client
