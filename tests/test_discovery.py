"""Tests for tools/discovery.py — discovery inspection tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.tools._utils import _clamp_limit, _clamp_offset, _validate_table_name
from servicenow_cmdb_mcp.tools.discovery import register_discovery_tools


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
    """Register discovery tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_discovery_tools(mcp, mock_client)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Unit tests: helpers ─────────────────────────────────────────────


class TestClampLimit:
    def test_within_range(self):
        assert _clamp_limit(25) == 25

    def test_below_min(self):
        assert _clamp_limit(0) == 1

    def test_above_max(self):
        assert _clamp_limit(9999) == 1000


class TestClampOffset:
    def test_negative(self):
        assert _clamp_offset(-5) == 0

    def test_zero(self):
        assert _clamp_offset(0) == 0


# ── list_discovery_schedules ────────────────────────────────────────


class TestListDiscoverySchedules:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["list_discovery_schedules"]())
        assert result["count"] == 0
        assert result["schedules"] == []

    @pytest.mark.asyncio
    async def test_pagination_signals(self, mock_client, tools):
        result = _parse(await tools["list_discovery_schedules"]())
        assert result["total_count"] == 0
        assert result["has_more"] is False
        assert result["next_offset"] == 0

    @pytest.mark.asyncio
    async def test_returns_schedules(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "s1",
                "name": "Nightly Scan",
                "active": "true",
                "discover": "IP Address",
                "run_as": "admin",
                "sys_updated_on": "2026-01-01 00:00:00",
            }
        ]
        result = _parse(await tools["list_discovery_schedules"]())
        assert result["count"] == 1
        s = result["schedules"][0]
        assert s["name"] == "Nightly Scan"
        assert s["active"] == "true"
        assert s["discover"] == "IP Address"

    @pytest.mark.asyncio
    async def test_active_only_filter(self, mock_client, tools):
        await tools["list_discovery_schedules"](active_only=True)
        call_args = mock_client.get_records.call_args
        assert "active=true" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_all_schedules(self, mock_client, tools):
        await tools["list_discovery_schedules"](active_only=False)
        call_args = mock_client.get_records.call_args
        assert "active=true" not in call_args.kwargs.get("query", "")

    @pytest.mark.asyncio
    async def test_limit_clamped(self, mock_client, tools):
        await tools["list_discovery_schedules"](limit=9999)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_offset_clamped(self, mock_client, tools):
        await tools["list_discovery_schedules"](offset=-10)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["offset"] == 0

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["list_discovery_schedules"]())
        assert result["error"] is True
        assert result["category"] == "PermissionError"


# ── get_discovery_status ────────────────────────────────────────────


class TestGetDiscoveryStatus:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_discovery_status"]())
        assert result["count"] == 0
        assert result["statuses"] == []

    @pytest.mark.asyncio
    async def test_returns_statuses(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "ds1",
                "state": "Completed",
                "source": "ServiceNow",
                "ip_address": "10.0.1.1",
                "dsc_schedule": "Nightly Scan",
                "cmdb_ci": "ci123",
                "started": "2026-01-01 00:00:00",
                "completed": "2026-01-01 00:05:00",
                "sys_created_on": "2026-01-01 00:00:00",
            }
        ]
        result = _parse(await tools["get_discovery_status"]())
        assert result["count"] == 1
        s = result["statuses"][0]
        assert s["state"] == "Completed"
        assert s["ip_address"] == "10.0.1.1"

    @pytest.mark.asyncio
    async def test_filter_by_schedule(self, mock_client, tools):
        await tools["get_discovery_status"](schedule_name="Nightly")
        call_args = mock_client.get_records.call_args
        assert "dsc_scheduleSTARTSWITHNightly" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_filter_by_state(self, mock_client, tools):
        await tools["get_discovery_status"](state="Error")
        call_args = mock_client.get_records.call_args
        assert "state=Error" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_query_injection_blocked(self, tools):
        result = _parse(await tools["get_discovery_status"](schedule_name="test^active=false"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_state_injection_blocked(self, tools):
        result = _parse(await tools["get_discovery_status"](state="Error^ORactive=true"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_invalid_state_rejected(self, tools):
        result = _parse(await tools["get_discovery_status"](state="BadState"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"
        assert "Invalid state" in result["message"]

    @pytest.mark.asyncio
    async def test_valid_states_accepted(self, mock_client, tools):
        for state in ("Starting", "Active", "Completed", "Cancelled", "Error"):
            await tools["get_discovery_status"](state=state)
            call_args = mock_client.get_records.call_args
            assert f"state={state}" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_orders_desc(self, mock_client, tools):
        await tools["get_discovery_status"]()
        call_args = mock_client.get_records.call_args
        assert "DESC" in call_args.kwargs["order_by"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_discovery_status"]())
        assert result["error"] is True


# ── get_discovery_errors ────────────────────────────────────────────


class TestGetDiscoveryErrors:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_discovery_errors"]())
        assert result["count"] == 0
        assert result["errors"] == []
        assert result["days_back"] == 7

    @pytest.mark.asyncio
    async def test_returns_errors(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "e1",
                "level": "Error",
                "message": "Connection refused",
                "source": "10.0.1.1",
                "cmdb_ci": "",
                "status": "ds1",
                "sys_created_on": "2026-01-01 00:00:00",
            }
        ]
        result = _parse(await tools["get_discovery_errors"]())
        assert result["count"] == 1
        e = result["errors"][0]
        assert e["level"] == "Error"
        assert e["message"] == "Connection refused"

    @pytest.mark.asyncio
    async def test_default_error_filter(self, mock_client, tools):
        """Without severity param, defaults to Error level."""
        await tools["get_discovery_errors"]()
        call_args = mock_client.get_records.call_args
        assert "level=Error" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_custom_severity(self, mock_client, tools):
        await tools["get_discovery_errors"](severity="Warning")
        call_args = mock_client.get_records.call_args
        assert "level=Warning" in call_args.kwargs["query"]
        assert "level=Error" not in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_in_query(self, mock_client, tools):
        await tools["get_discovery_errors"](days=30)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(30)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped_min(self, mock_client, tools):
        await tools["get_discovery_errors"](days=0)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(1)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped_max(self, mock_client, tools):
        await tools["get_discovery_errors"](days=9999)
        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(365)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_invalid_severity_rejected(self, tools):
        result = _parse(await tools["get_discovery_errors"](severity="Critical"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"
        assert "Invalid severity" in result["message"]

    @pytest.mark.asyncio
    async def test_valid_severities_accepted(self, mock_client, tools):
        for sev in ("Error", "Warning", "Info"):
            await tools["get_discovery_errors"](severity=sev)
            call_args = mock_client.get_records.call_args
            assert f"level={sev}" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_back_in_response(self, mock_client, tools):
        result = _parse(await tools["get_discovery_errors"](days=14))
        assert result["days_back"] == 14

    @pytest.mark.asyncio
    async def test_orders_desc(self, mock_client, tools):
        await tools["get_discovery_errors"]()
        call_args = mock_client.get_records.call_args
        assert "DESC" in call_args.kwargs["order_by"]

    @pytest.mark.asyncio
    async def test_message_truncation(self, mock_client, tools):
        """Long messages should be truncated with length metadata."""
        long_msg = "x" * 1000
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "level": "Error", "message": long_msg,
            "source": "10.0.1.1", "cmdb_ci": "", "status": "ds1",
            "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_discovery_errors"](max_message_length=500))
        e = result["errors"][0]
        assert len(e["message"]) == 501  # 500 chars + ellipsis
        assert e["message"].endswith("\u2026")
        assert e["message_length"] == 1000

    @pytest.mark.asyncio
    async def test_no_truncation_when_short(self, mock_client, tools):
        """Short messages should not be truncated."""
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "level": "Error", "message": "Short error",
            "source": "10.0.1.1", "cmdb_ci": "", "status": "ds1",
            "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_discovery_errors"](max_message_length=500))
        e = result["errors"][0]
        assert e["message"] == "Short error"
        assert "message_length" not in e

    @pytest.mark.asyncio
    async def test_truncation_disabled(self, mock_client, tools):
        """max_message_length=0 should disable truncation."""
        long_msg = "x" * 1000
        mock_client.get_records.return_value = [{
            "sys_id": "e1", "level": "Error", "message": long_msg,
            "source": "10.0.1.1", "cmdb_ci": "", "status": "ds1",
            "sys_created_on": "2026-01-01",
        }]
        result = _parse(await tools["get_discovery_errors"](max_message_length=0))
        e = result["errors"][0]
        assert e["message"] == long_msg
        assert "message_length" not in e

    @pytest.mark.asyncio
    async def test_all_severities_when_empty_string(self, mock_client, tools):
        """severity='' should not filter by level."""
        await tools["get_discovery_errors"](severity="")
        call_args = mock_client.get_records.call_args
        assert "level=" not in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_discovery_errors"]())
        assert result["error"] is True
