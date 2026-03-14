"""Tests for tools/queries.py — CI query, inspect, count, and schema discovery tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.errors import NotFoundError, SNPermissionError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _validate_cmdb_table,
    _validate_sys_id,
    _validate_table_name,
)
from servicenow_cmdb_mcp.tools.queries import register_query_tools


# ── Fake sys_ids ────────────────────────────────────────────────────

CI_A = "a" * 32
CI_B = "b" * 32


# ── Helpers ─────────────────────────────────────────────────────────


def _ci_record(sys_id: str, name: str, cls: str = "cmdb_ci_server") -> dict:
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": cls,
        "operational_status": "1",
        "ip_address": "10.0.0.1",
        "location": "",
        "sys_updated_on": "2025-01-01 00:00:00",
    }


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value=None)
    client.get_aggregate = AsyncMock(return_value={"result": {"stats": {"count": "0"}}})
    return client


@pytest.fixture
def cache() -> MetadataCache:
    return MetadataCache(ttl=3600)


@pytest.fixture
def tools(mock_client, cache):
    """Register query tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_query_tools(mcp, mock_client, cache)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Validation utilities ────────────────────────────────────────────


class TestValidateTableName:
    def test_valid_table(self):
        assert _validate_table_name("cmdb_ci_server") is None

    def test_empty(self):
        assert _validate_table_name("") is not None

    def test_whitespace(self):
        assert _validate_table_name("   ") is not None

    def test_special_chars(self):
        assert _validate_table_name("cmdb_ci; DROP TABLE") is not None

    def test_path_traversal(self):
        assert _validate_table_name("../etc/passwd") is not None

    def test_unicode_homoglyph(self):
        # Cyrillic 'а' looks like Latin 'a' but should be rejected
        assert _validate_table_name("cmdb_ci_server\u0430") is not None


class TestValidateCmdbTable:
    def test_valid_cmdb_table(self):
        assert _validate_cmdb_table("cmdb_ci_server") is None

    def test_valid_cmdb_ci(self):
        assert _validate_cmdb_table("cmdb_ci") is None

    def test_valid_cmdb_rel_ci(self):
        assert _validate_cmdb_table("cmdb_rel_ci") is None

    def test_rejects_sys_user(self):
        err = _validate_cmdb_table("sys_user")
        assert err is not None
        assert "not a CMDB table" in err

    def test_rejects_sys_user_has_role(self):
        assert _validate_cmdb_table("sys_user_has_role") is not None

    def test_rejects_sys_properties(self):
        assert _validate_cmdb_table("sys_properties") is not None

    def test_format_error_takes_priority(self):
        err = _validate_cmdb_table("invalid!table")
        assert err is not None
        assert "ASCII" in err


class TestValidateSysId:
    def test_valid_32_hex(self):
        assert _validate_sys_id(CI_A) is None

    def test_empty(self):
        assert _validate_sys_id("") is not None

    def test_path_traversal(self):
        assert _validate_sys_id("../../etc/passwd") is not None

    def test_special_chars(self):
        assert _validate_sys_id("abc123;DROP") is not None

    def test_short_alphanumeric(self):
        assert _validate_sys_id("abc123") is None


class TestClampLimit:
    def test_normal(self):
        assert _clamp_limit(25) == 25

    def test_zero(self):
        assert _clamp_limit(0) == 1

    def test_negative(self):
        assert _clamp_limit(-5) == 1

    def test_over_max(self):
        assert _clamp_limit(2000) == 1000

    def test_at_max(self):
        assert _clamp_limit(1000) == 1000


class TestClampOffset:
    def test_normal(self):
        assert _clamp_offset(10) == 10

    def test_zero(self):
        assert _clamp_offset(0) == 0

    def test_negative(self):
        assert _clamp_offset(-5) == 0


# ── search_cis ──────────────────────────────────────────────────────


class TestSearchCis:
    @pytest.mark.asyncio
    async def test_returns_records(self, mock_client, tools):
        records = [_ci_record(CI_A, "web-01"), _ci_record(CI_B, "web-02")]
        mock_client.get_records.return_value = records
        result = _parse(await tools["search_cis"](ci_class="cmdb_ci_server"))
        assert result["count"] == 2
        assert result["records"][0]["name"] == "web-01"

    @pytest.mark.asyncio
    async def test_invalid_table_returns_error(self, tools):
        result = _parse(await tools["search_cis"](ci_class="sys_user"))
        assert result["error"] is True
        assert "not a CMDB table" in result["message"]

    @pytest.mark.asyncio
    async def test_name_filter_uses_startswith(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["search_cis"](ci_class="cmdb_ci", name_filter="web")
        call_kwargs = mock_client.get_records.call_args.kwargs
        assert "nameSTARTSWITH" in call_kwargs["query"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["search_cis"]())
        assert result["error"] is True
        assert result["category"] == "PermissionError"

    @pytest.mark.asyncio
    async def test_default_fields(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["search_cis"]()
        call_kwargs = mock_client.get_records.call_args.kwargs
        assert "sys_id" in call_kwargs["fields"]
        assert "name" in call_kwargs["fields"]

    @pytest.mark.asyncio
    async def test_custom_fields(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["search_cis"](fields=["sys_id", "name"])
        call_kwargs = mock_client.get_records.call_args.kwargs
        assert call_kwargs["fields"] == ["sys_id", "name"]


# ── query_cis_raw ───────────────────────────────────────────────────


class TestQueryCisRaw:
    @pytest.mark.asyncio
    async def test_passes_raw_query(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["query_cis_raw"](
            table="cmdb_ci_server",
            encoded_query="nameSTARTSWITHweb^operational_status=1",
        )
        call_kwargs = mock_client.get_records.call_args.kwargs
        assert call_kwargs["query"] == "nameSTARTSWITHweb^operational_status=1"

    @pytest.mark.asyncio
    async def test_rejects_non_cmdb_table(self, tools):
        result = _parse(await tools["query_cis_raw"](
            table="sys_user", encoded_query="active=true",
        ))
        assert result["error"] is True
        assert "not a CMDB table" in result["message"]

    @pytest.mark.asyncio
    async def test_returns_results(self, mock_client, tools):
        records = [_ci_record(CI_A, "web-01")]
        mock_client.get_records.return_value = records
        result = _parse(await tools["query_cis_raw"](
            table="cmdb_ci", encoded_query="",
        ))
        assert result["count"] == 1


# ── get_ci_details ──────────────────────────────────────────────────


class TestGetCiDetails:
    @pytest.mark.asyncio
    async def test_returns_record(self, mock_client, tools):
        record = {
            "sys_id": CI_A, "name": "web-01", "sys_class_name": "cmdb_ci_server",
            "operational_status": "1", "ip_address": "10.0.0.1",
        }
        mock_client.get_record.return_value = record
        result = _parse(await tools["get_ci_details"](sys_id=CI_A, table="cmdb_ci_server"))
        assert result["name"] == "web-01"

    @pytest.mark.asyncio
    async def test_not_found(self, mock_client, tools):
        mock_client.get_record.side_effect = NotFoundError("Not found")
        result = _parse(await tools["get_ci_details"](sys_id=CI_A, table="cmdb_ci"))
        assert result["error"] is True
        assert result["category"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_invalid_sys_id(self, tools):
        result = _parse(await tools["get_ci_details"](sys_id="../etc", table="cmdb_ci"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_ci_details"](sys_id=CI_A, table="sys_user"))
        assert result["error"] is True
        assert "not a CMDB table" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_sys_id(self, tools):
        result = _parse(await tools["get_ci_details"](sys_id="", table="cmdb_ci"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_record.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_ci_details"](sys_id=CI_A, table="cmdb_ci"))
        assert result["error"] is True


# ── count_cis ───────────────────────────────────────────────────────


class TestCountCis:
    @pytest.mark.asyncio
    async def test_returns_count(self, mock_client, tools):
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "42"}}}
        result = _parse(await tools["count_cis"](table="cmdb_ci"))
        assert result["stats"]["count"] == "42"

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["count_cis"](table="sys_user"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_with_group_by(self, mock_client, tools):
        mock_client.get_aggregate.return_value = {"result": []}
        await tools["count_cis"](table="cmdb_ci", group_by="sys_class_name")
        call_kwargs = mock_client.get_aggregate.call_args.kwargs
        assert call_kwargs["group_by"] == "sys_class_name"

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_aggregate.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["count_cis"]())
        assert result["error"] is True


# ── suggest_table ───────────────────────────────────────────────────


class TestSuggestTable:
    @pytest.mark.asyncio
    async def test_finds_matching_classes(self, mock_client, cache, tools):
        mock_client.get_records.return_value = [
            {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci"},
            {"name": "cmdb_ci_linux_server", "label": "Linux Server", "super_class": "cmdb_ci_server"},
            {"name": "cmdb_ci_appl", "label": "Application", "super_class": "cmdb_ci"},
        ]
        result = _parse(await tools["suggest_table"](description="linux servers"))
        assert result["suggestion_count"] > 0
        # Linux server should score highest
        assert result["suggestions"][0]["table"] == "cmdb_ci_linux_server"

    @pytest.mark.asyncio
    async def test_no_matches(self, mock_client, cache, tools):
        mock_client.get_records.return_value = [
            {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci"},
        ]
        result = _parse(await tools["suggest_table"](description="quantum teleporter"))
        assert result["suggestions"] == []

    @pytest.mark.asyncio
    async def test_short_description(self, tools):
        result = _parse(await tools["suggest_table"](description="a b"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_caches_classes(self, mock_client, cache, tools):
        mock_client.get_records.return_value = [
            {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci"},
        ]
        await tools["suggest_table"](description="server machine")
        await tools["suggest_table"](description="linux server")
        # Second call should hit cache — only 1 API call
        assert mock_client.get_records.call_count == 1


# ── list_ci_classes ─────────────────────────────────────────────────


class TestListCiClasses:
    @pytest.mark.asyncio
    async def test_returns_classes(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci", "sys_id": "s1"},
            {"name": "cmdb_ci_vm", "label": "Virtual Machine", "super_class": "cmdb_ci", "sys_id": "s2"},
        ]
        result = _parse(await tools["list_ci_classes"]())
        assert result["count"] == 2
        assert result["classes"][0]["name"] == "cmdb_ci_server"

    @pytest.mark.asyncio
    async def test_caches_results(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {"name": "cmdb_ci_server", "label": "Server", "super_class": "cmdb_ci", "sys_id": "s1"},
        ]
        await tools["list_ci_classes"]()
        result = _parse(await tools["list_ci_classes"]())
        assert result["cached"] is True
        assert mock_client.get_records.call_count == 1

    @pytest.mark.asyncio
    async def test_limit_slices_results(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {"name": f"cmdb_ci_{i}", "label": f"CI {i}", "super_class": "cmdb_ci", "sys_id": f"s{i}"}
            for i in range(10)
        ]
        result = _parse(await tools["list_ci_classes"](limit=3))
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["list_ci_classes"]())
        assert result["error"] is True


# ── describe_ci_class ───────────────────────────────────────────────


class TestDescribeCiClass:
    @pytest.mark.asyncio
    async def test_returns_fields(self, mock_client, tools):
        mock_client.get_records.side_effect = [
            # sys_db_object hierarchy walk
            [{"super_class": ""}],
            # sys_dictionary fields
            [
                {"name": "cmdb_ci_server", "element": "name", "column_label": "Name",
                 "internal_type": "string", "max_length": "100", "mandatory": "true",
                 "reference": "", "default_value": ""},
            ],
            # cmdb_rel_type_suggest
            [],
        ]
        result = _parse(await tools["describe_ci_class"](class_name="cmdb_ci_server"))
        assert result["class_name"] == "cmdb_ci_server"
        assert result["field_count"] == 1
        assert result["fields"][0]["name"] == "name"
        assert result["fields"][0]["mandatory"] is True

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["describe_ci_class"](class_name="cmdb_ci_server"))
        assert result["error"] is True
