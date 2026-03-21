"""Tests for tools/imports.py — import inspection tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.tools._utils import _clamp_limit, _clamp_offset, _validate_table_name
from servicenow_cmdb_mcp.tools.imports import register_import_tools


# ── Helpers ─────────────────────────────────────────────────────────


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_records = AsyncMock(return_value=[])
    client.get_aggregate = AsyncMock(return_value={"result": {"stats": {"count": "0"}}})
    return client


@pytest.fixture
def tools(mock_client):
    """Register import tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_import_tools(mcp, mock_client)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── list_data_sources ───────────────────────────────────────────────


class TestListDataSources:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["list_data_sources"]())
        assert result["count"] == 0
        assert result["data_sources"] == []
        assert result["target_table_filter"] == "(all)"

    @pytest.mark.asyncio
    async def test_pagination_signals(self, mock_client, tools):
        result = _parse(await tools["list_data_sources"]())
        assert result["total_count"] == 0
        assert result["has_more"] is False
        assert result["next_offset"] == 0

    @pytest.mark.asyncio
    async def test_returns_sources(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "ds1",
                "name": "LDAP Import",
                "import_set_table_name": "u_ldap_import",
                "type": "LDAP",
                "active": "true",
                "sys_updated_on": "2026-01-01 00:00:00",
            }
        ]
        result = _parse(await tools["list_data_sources"]())
        assert result["count"] == 1
        ds = result["data_sources"][0]
        assert ds["name"] == "LDAP Import"
        assert ds["type"] == "LDAP"

    @pytest.mark.asyncio
    async def test_filter_by_target_table(self, mock_client, tools):
        await tools["list_data_sources"](target_table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "import_set_table_nameCONTAINScmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_active_filter(self, mock_client, tools):
        await tools["list_data_sources"](active_only=True)
        call_args = mock_client.get_records.call_args
        assert "active=true" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_all_sources(self, mock_client, tools):
        await tools["list_data_sources"](active_only=False)
        call_args = mock_client.get_records.call_args
        assert "active=true" not in call_args.kwargs.get("query", "")

    @pytest.mark.asyncio
    async def test_invalid_target_table(self, tools):
        result = _parse(await tools["list_data_sources"](target_table="bad/table"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_limit_clamped(self, mock_client, tools):
        await tools["list_data_sources"](limit=9999)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["list_data_sources"]())
        assert result["error"] is True


# ── get_import_set_runs ─────────────────────────────────────────────


class TestGetImportSetRuns:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_import_set_runs"]())
        assert result["count"] == 0
        assert result["import_set_runs"] == []
        assert result["days_back"] == 7

    @pytest.mark.asyncio
    async def test_returns_runs(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "r1",
                "table_name": "u_ldap_import",
                "state": "Processed",
                "count": "100",
                "insert_count": "50",
                "update_count": "45",
                "error_count": "5",
                "data_source": "LDAP Import",
                "sys_created_on": "2026-01-01 00:00:00",
                "completed": "2026-01-01 00:05:00",
            }
        ]
        result = _parse(await tools["get_import_set_runs"]())
        assert result["count"] == 1
        run = result["import_set_runs"][0]
        assert run["state"] == "Processed"
        assert run["total_rows"] == "100"
        assert run["error_count"] == "5"

    @pytest.mark.asyncio
    async def test_filter_by_table(self, mock_client, tools):
        await tools["get_import_set_runs"](table_name="u_ldap")
        call_args = mock_client.get_records.call_args
        assert "table_nameSTARTSWITHu_ldap" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_filter_by_state(self, mock_client, tools):
        await tools["get_import_set_runs"](state="Error")
        call_args = mock_client.get_records.call_args
        assert "state=Error" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_in_query(self, mock_client, tools):
        await tools["get_import_set_runs"](days=30)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(30)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped_min(self, mock_client, tools):
        await tools["get_import_set_runs"](days=0)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(1)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped_max(self, mock_client, tools):
        await tools["get_import_set_runs"](days=9999)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(365)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_table_injection_blocked(self, tools):
        result = _parse(await tools["get_import_set_runs"](table_name="test^evil"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_invalid_table_name(self, tools):
        result = _parse(await tools["get_import_set_runs"](table_name="bad/table"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_state_injection_blocked(self, tools):
        result = _parse(await tools["get_import_set_runs"](state="Error^ORactive=true"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_orders_desc(self, mock_client, tools):
        await tools["get_import_set_runs"]()
        call_args = mock_client.get_records.call_args
        assert "DESC" in call_args.kwargs["order_by"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_import_set_runs"]())
        assert result["error"] is True


# ── get_transform_errors ────────────────────────────────────────────


class TestGetTransformErrors:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_transform_errors"]())
        assert result["count"] == 0
        assert result["transform_errors"] == []
        assert result["days_back"] == 7

    @pytest.mark.asyncio
    async def test_returns_errors(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "e1",
                "sys_import_set": "is1",
                "sys_transform_map": "tm1",
                "sys_target_table": "cmdb_ci_server",
                "sys_target_sys_id": "",
                "error_message": "Mandatory field 'name' is empty",
                "status": "error",
                "sys_created_on": "2026-01-01 00:00:00",
            }
        ]
        result = _parse(await tools["get_transform_errors"]())
        assert result["count"] == 1
        err = result["transform_errors"][0]
        assert err["error_message"] == "Mandatory field 'name' is empty"
        assert err["target_table"] == "cmdb_ci_server"

    @pytest.mark.asyncio
    async def test_filters_by_status_error(self, mock_client, tools):
        """Should always filter for status=error."""
        await tools["get_transform_errors"]()
        call_args = mock_client.get_records.call_args
        assert "status=error" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_filter_by_target_table(self, mock_client, tools):
        await tools["get_transform_errors"](target_table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "sys_target_tableSTARTSWITHcmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_in_query(self, mock_client, tools):
        await tools["get_transform_errors"](days=14)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(14)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped(self, mock_client, tools):
        result = _parse(await tools["get_transform_errors"](days=0))
        assert result["days_back"] == 1

    @pytest.mark.asyncio
    async def test_invalid_target_table(self, tools):
        result = _parse(await tools["get_transform_errors"](target_table="bad;drop"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_orders_desc(self, mock_client, tools):
        await tools["get_transform_errors"]()
        call_args = mock_client.get_records.call_args
        assert "DESC" in call_args.kwargs["order_by"]

    @pytest.mark.asyncio
    async def test_error_message_truncation(self, mock_client, tools):
        """Long error messages should be truncated with length metadata."""
        long_msg = "x" * 1000
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "sys_import_set": "is1",
            "sys_transform_map": "tm1", "sys_target_table": "cmdb_ci",
            "sys_target_sys_id": "", "error_message": long_msg,
            "status": "error", "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_transform_errors"](max_error_length=500))
        err = result["transform_errors"][0]
        assert len(err["error_message"]) == 501  # 500 + ellipsis
        assert err["error_message"].endswith("\u2026")
        assert err["error_message_length"] == 1000

    @pytest.mark.asyncio
    async def test_no_truncation_when_short(self, mock_client, tools):
        """Short messages should not be truncated."""
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "sys_import_set": "is1",
            "sys_transform_map": "tm1", "sys_target_table": "cmdb_ci",
            "sys_target_sys_id": "", "error_message": "Short error",
            "status": "error", "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_transform_errors"](max_error_length=500))
        err = result["transform_errors"][0]
        assert err["error_message"] == "Short error"
        assert "error_message_length" not in err

    @pytest.mark.asyncio
    async def test_truncation_disabled(self, mock_client, tools):
        """max_error_length=0 should disable truncation."""
        long_msg = "x" * 1000
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "sys_import_set": "is1",
            "sys_transform_map": "tm1", "sys_target_table": "cmdb_ci",
            "sys_target_sys_id": "", "error_message": long_msg,
            "status": "error", "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_transform_errors"](max_error_length=0))
        err = result["transform_errors"][0]
        assert err["error_message"] == long_msg
        assert "error_message_length" not in err

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_transform_errors"]())
        assert result["error"] is True
