"""Tests for resources/schema.py — MCP Resources and refresh_metadata_cache tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.resources.schema import (
    _fetch_all_classes,
    _fetch_instance_metadata,
    _fetch_relationship_types,
    register_schema_resources,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value={"result": []})
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value=None)
    client.get_aggregate = AsyncMock(return_value={"result": {"stats": {"count": "0"}}})
    return client


@pytest.fixture
def cache() -> MetadataCache:
    return MetadataCache(ttl=3600)


@pytest.fixture
def tools_and_resources(mock_client, cache):
    """Register resources + tool and return callable functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_schema_resources(mcp, mock_client, cache)

    result = {}
    # Resources
    for uri, resource in mcp._resource_manager._resources.items():
        result[str(uri)] = resource
    # Templates
    for uri_template, template in mcp._resource_manager._templates.items():
        result[str(uri_template)] = template
    # Tools
    for tool in mcp._tool_manager._tools.values():
        result[tool.fn.__name__] = tool.fn
    return result


# ── _fetch_all_classes ──────────────────────────────────────────────


class TestFetchAllClasses:
    @pytest.mark.asyncio
    async def test_returns_classes(self, mock_client, cache):
        mock_client.get.return_value = {
            "result": [
                {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci"},
                {"name": "cmdb_ci_vm", "label": "Virtual Machine", "super_class": "cmdb_ci"},
            ]
        }
        data = await _fetch_all_classes(mock_client, cache)
        assert len(data["classes"]) == 2
        assert data["classes"][0]["name"] == "cmdb_ci_server"
        assert data["classes"][0]["parent"] == "cmdb_ci"
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_uses_display_value_param(self, mock_client, cache):
        mock_client.get.return_value = {"result": []}
        await _fetch_all_classes(mock_client, cache)
        call_args = mock_client.get.call_args
        assert call_args.args[0] == "/api/now/table/sys_db_object"
        assert call_args.kwargs["params"]["sysparm_display_value"] == "true"

    @pytest.mark.asyncio
    async def test_caches_result(self, mock_client, cache):
        mock_client.get.return_value = {"result": [{"name": "cmdb_ci", "label": "CI", "super_class": ""}]}
        await _fetch_all_classes(mock_client, cache)
        # Second call should hit cache
        await _fetch_all_classes(mock_client, cache)
        assert mock_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, cache):
        mock_client.get.return_value = {"result": []}
        data = await _fetch_all_classes(mock_client, cache)
        assert data["classes"] == []
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncation_flag(self, mock_client, cache):
        mock_client.get.return_value = {
            "result": [{"name": f"cmdb_ci_{i}", "label": f"CI {i}", "super_class": "cmdb_ci"} for i in range(1000)]
        }
        data = await _fetch_all_classes(mock_client, cache)
        assert data["truncated"] is True


# ── _fetch_relationship_types ───────────────────────────────────────


class TestFetchRelationshipTypes:
    @pytest.mark.asyncio
    async def test_returns_types(self, mock_client, cache):
        mock_client.get_records.return_value = [
            {
                "sys_id": "rt1",
                "name": "Depends on::Used by",
                "parent_descriptor": "Depends on",
                "child_descriptor": "Used by",
            }
        ]
        types = await _fetch_relationship_types(mock_client, cache)
        assert len(types) == 1
        assert types[0]["name"] == "Depends on::Used by"

    @pytest.mark.asyncio
    async def test_shares_cache_key_with_tools(self, mock_client, cache):
        """Uses rel_types:all — same key as list_relationship_types tool."""
        data = [{"sys_id": "rt1", "name": "test", "parent_descriptor": "p", "child_descriptor": "c"}]
        cache.set("rel_types:all", data)
        types = await _fetch_relationship_types(mock_client, cache)
        assert types == data
        assert mock_client.get_records.call_count == 0

    @pytest.mark.asyncio
    async def test_caches_result(self, mock_client, cache):
        mock_client.get_records.return_value = []
        await _fetch_relationship_types(mock_client, cache)
        await _fetch_relationship_types(mock_client, cache)
        assert mock_client.get_records.call_count == 1


# ── _fetch_instance_metadata ───────────────────────────────────────


class TestFetchInstanceMetadata:
    @pytest.mark.asyncio
    async def test_returns_metadata(self, mock_client, cache):
        mock_client.get_records.side_effect = [
            # sys_properties
            [{"name": "glide.buildname", "value": "Xanadu"}],
            # v_plugin
            [{"name": "CMDB CI Class Models", "version": "1.0", "active": "active"}],
        ]
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "5000"}}}
        result = await _fetch_instance_metadata(mock_client, cache)
        assert result["version"] == "Xanadu"
        assert result["ci_count"] == 5000
        assert len(result["cmdb_plugins"]) == 1
        assert "errors" not in result

    @pytest.mark.asyncio
    async def test_fallback_to_glide_war(self, mock_client, cache):
        mock_client.get_records.side_effect = [
            [{"name": "glide.war", "value": "glide-xanadu-06-24-2025"}],
            [],
        ]
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "0"}}}
        result = await _fetch_instance_metadata(mock_client, cache)
        assert result["version"] == "glide-xanadu-06-24-2025"

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_client, cache):
        from servicenow_cmdb_mcp.errors import SNPermissionError

        mock_client.get_records.side_effect = [
            [{"name": "glide.buildname", "value": "Xanadu"}],
            SNPermissionError("No access to v_plugin"),
        ]
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "100"}}}
        result = await _fetch_instance_metadata(mock_client, cache)
        assert result["version"] == "Xanadu"
        assert result["ci_count"] == 100
        assert result["cmdb_plugins"] == []
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_caches_result(self, mock_client, cache):
        mock_client.get_records.side_effect = [[], []]
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "0"}}}
        await _fetch_instance_metadata(mock_client, cache)
        # Second call should hit cache
        await _fetch_instance_metadata(mock_client, cache)
        assert mock_client.get_records.call_count == 2  # only first call
        assert mock_client.get_aggregate.call_count == 1

    @pytest.mark.asyncio
    async def test_all_arms_fail(self, mock_client, cache):
        from servicenow_cmdb_mcp.errors import SNPermissionError

        mock_client.get_records.side_effect = SNPermissionError("Denied")
        mock_client.get_aggregate.side_effect = SNPermissionError("Denied")
        result = await _fetch_instance_metadata(mock_client, cache)
        assert result["version"] == ""
        assert result["ci_count"] == 0
        assert len(result["errors"]) == 3


# ── Resource registration ──────────────────────────────────────────


class TestResourceRegistration:
    def test_registers_static_resources(self, tools_and_resources):
        assert "cmdb://schema/classes" in tools_and_resources
        assert "cmdb://schema/relationship-types" in tools_and_resources
        assert "cmdb://instance/metadata" in tools_and_resources

    def test_registers_template_resource(self, tools_and_resources):
        assert "cmdb://schema/classes/{class_name}" in tools_and_resources

    def test_registers_refresh_tool(self, tools_and_resources):
        assert "refresh_metadata_cache" in tools_and_resources


# ── refresh_metadata_cache tool ─────────────────────────────────────


class TestRefreshMetadataCache:
    @pytest.mark.asyncio
    async def test_clears_cache(self, mock_client, cache, tools_and_resources):
        cache.set("resource:classes", [{"name": "test"}])
        cache.set("rel_types:all", [{"name": "test"}])
        cache.set("ci_class_desc:cmdb_ci", {"test": True})

        result = _parse(await tools_and_resources["refresh_metadata_cache"]())
        assert result["cleared"] is True

        # All keys should be gone
        assert cache.get("resource:classes") is None
        assert cache.get("rel_types:all") is None
        assert cache.get("ci_class_desc:cmdb_ci") is None

    @pytest.mark.asyncio
    async def test_idempotent(self, mock_client, cache, tools_and_resources):
        result1 = _parse(await tools_and_resources["refresh_metadata_cache"]())
        result2 = _parse(await tools_and_resources["refresh_metadata_cache"]())
        assert result1["cleared"] is True
        assert result2["cleared"] is True


# ── Resource error handling ─────────────────────────────────────────


class TestResourceErrorHandling:
    @pytest.mark.asyncio
    async def test_classes_service_now_error(self, mock_client, cache):
        from servicenow_cmdb_mcp.errors import SNPermissionError

        mock_client.get.side_effect = SNPermissionError("Denied")
        # Call the fetch function directly (resources return str, harder to test via MCP)
        try:
            await _fetch_all_classes(mock_client, cache)
            assert False, "Should have raised"
        except SNPermissionError:
            pass

    @pytest.mark.asyncio
    async def test_relationship_types_error(self, mock_client, cache):
        from servicenow_cmdb_mcp.errors import SNPermissionError

        mock_client.get_records.side_effect = SNPermissionError("Denied")
        try:
            await _fetch_relationship_types(mock_client, cache)
            assert False, "Should have raised"
        except SNPermissionError:
            pass
