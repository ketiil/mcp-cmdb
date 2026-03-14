"""Tests for tools/configurables.py — configurable inspection tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.tools.configurables import (
    _clamp_limit,
    _clamp_offset,
    _redact_script_fields,
    _validate_table_name,
    register_configurable_tools,
)


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
    """Register configurable tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_configurable_tools(mcp, mock_client)

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
        assert _clamp_limit(-5) == 1

    def test_above_max(self):
        assert _clamp_limit(9999) == 1000

    def test_at_boundaries(self):
        assert _clamp_limit(1) == 1
        assert _clamp_limit(1000) == 1000


class TestClampOffset:
    def test_positive(self):
        assert _clamp_offset(10) == 10

    def test_negative(self):
        assert _clamp_offset(-5) == 0

    def test_zero(self):
        assert _clamp_offset(0) == 0


class TestValidateTableName:
    def test_valid(self):
        assert _validate_table_name("cmdb_ci_server") is None

    def test_empty(self):
        assert _validate_table_name("") is not None

    def test_blank(self):
        assert _validate_table_name("   ") is not None

    def test_path_traversal(self):
        err = _validate_table_name("cmdb_ci/../sys_user")
        assert err is not None
        assert "Invalid table name" in err

    def test_special_chars(self):
        assert _validate_table_name("table; DROP") is not None

    def test_slash(self):
        assert _validate_table_name("cmdb_ci/foo") is not None


class TestRedactScriptFields:
    def test_no_script(self):
        record = {"name": "test"}
        result = _redact_script_fields(record, ["script"])
        assert result["name"] == "test"

    def test_empty_script(self):
        record = {"script": ""}
        result = _redact_script_fields(record, ["script"])
        assert result["script"] == ""

    def test_non_string_script(self):
        record = {"script": 123}
        result = _redact_script_fields(record, ["script"])
        assert result["script"] == 123

    def test_does_not_mutate_original(self):
        record = {"name": "test", "script": "var x = 1;"}
        _ = _redact_script_fields(record, ["script"])
        assert record["script"] == "var x = 1;"


# ── get_business_rules ──────────────────────────────────────────────


class TestGetBusinessRules:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        mock_client.get_records.return_value = []
        result = _parse(await tools["get_business_rules"](table="cmdb_ci_server"))
        assert result["table"] == "cmdb_ci_server"
        assert result["count"] == 0
        assert result["business_rules"] == []

    @pytest.mark.asyncio
    async def test_returns_rules(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "abc123",
                "name": "Set Status",
                "collection": "cmdb_ci_server",
                "active": "true",
                "when": "before",
                "action_insert": "true",
                "action_update": "true",
                "action_delete": "false",
                "action_query": "false",
                "order": "100",
                "condition": "",
                "script": "current.status = 'active';",
            }
        ]
        result = _parse(await tools["get_business_rules"](table="cmdb_ci_server"))
        assert result["count"] == 1
        rule = result["business_rules"][0]
        assert rule["name"] == "Set Status"
        assert rule["when"] == "before"
        assert "insert" in rule["operations"]
        assert "update" in rule["operations"]
        assert "delete" not in rule["operations"]

    @pytest.mark.asyncio
    async def test_active_only_filter(self, mock_client, tools):
        await tools["get_business_rules"](table="cmdb_ci", active_only=True)
        call_args = mock_client.get_records.call_args
        assert "active=true" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_all_rules(self, mock_client, tools):
        await tools["get_business_rules"](table="cmdb_ci", active_only=False)
        call_args = mock_client.get_records.call_args
        assert "active=true" not in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_business_rules"](table="bad/table"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_empty_table(self, tools):
        result = _parse(await tools["get_business_rules"](table=""))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_limit_clamped(self, mock_client, tools):
        await tools["get_business_rules"](table="cmdb_ci", limit=9999)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_offset_clamped(self, mock_client, tools):
        await tools["get_business_rules"](table="cmdb_ci", offset=-10)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["offset"] == 0

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_business_rules"](table="cmdb_ci"))
        assert result["error"] is True
        assert result["category"] == "PermissionError"


# ── get_client_scripts ──────────────────────────────────────────────


class TestGetClientScripts:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_client_scripts"](table="cmdb_ci"))
        assert result["count"] == 0
        assert result["client_scripts"] == []

    @pytest.mark.asyncio
    async def test_returns_scripts(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "s1",
                "name": "OnLoad Script",
                "table": "cmdb_ci",
                "active": "true",
                "type": "onLoad",
                "field": "",
                "script": "alert('test');",
            }
        ]
        result = _parse(await tools["get_client_scripts"](table="cmdb_ci"))
        assert result["count"] == 1
        script = result["client_scripts"][0]
        assert script["name"] == "OnLoad Script"
        assert script["type"] == "onLoad"

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_client_scripts"](table="bad;table"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_active_filter(self, mock_client, tools):
        await tools["get_client_scripts"](table="cmdb_ci", active_only=True)
        call_args = mock_client.get_records.call_args
        assert "active=true" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_client_scripts"](table="cmdb_ci"))
        assert result["error"] is True


# ── get_flows ───────────────────────────────────────────────────────


class TestGetFlows:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_flows"](table="cmdb_ci"))
        assert result["count"] == 0
        assert result["flows"] == []

    @pytest.mark.asyncio
    async def test_returns_flows(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "f1",
                "name": "Auto Retire",
                "internal_name": "cmdb_ci_auto_retire",
                "description": "Retires stale CIs",
                "active": "true",
                "run_as": "system",
            }
        ]
        result = _parse(await tools["get_flows"](table="cmdb_ci"))
        assert result["count"] == 1
        flow = result["flows"][0]
        assert flow["name"] == "Auto Retire"
        assert flow["run_as"] == "system"

    @pytest.mark.asyncio
    async def test_contains_query(self, mock_client, tools):
        await tools["get_flows"](table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "internal_nameCONTAINScmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_flows"](table="../etc"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_flows"](table="cmdb_ci"))
        assert result["error"] is True


# ── get_acls ────────────────────────────────────────────────────────


class TestGetAcls:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_acls"](table="cmdb_ci"))
        assert result["count"] == 0
        assert result["acls"] == []

    @pytest.mark.asyncio
    async def test_returns_acls(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "acl1",
                "name": "cmdb_ci.read",
                "operation": "read",
                "type": "record",
                "active": "true",
                "admin_overrides": "true",
                "condition": "",
                "script": "",
            }
        ]
        result = _parse(await tools["get_acls"](table="cmdb_ci"))
        assert result["count"] == 1
        acl = result["acls"][0]
        assert acl["name"] == "cmdb_ci.read"
        assert acl["operation"] == "read"

    @pytest.mark.asyncio
    async def test_startswith_query(self, mock_client, tools):
        await tools["get_acls"](table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "nameSTARTSWITHcmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_acls"](table="x y z"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_acls"](table="cmdb_ci"))
        assert result["error"] is True


# ── analyze_configurables ──────────────────────────────────────────


class TestAnalyzeConfigurables:
    @pytest.mark.asyncio
    async def test_returns_counts(self, mock_client, tools):
        mock_client.get_aggregate.return_value = {"result": {"stats": {"count": "5"}}}
        result = _parse(await tools["analyze_configurables"](table="cmdb_ci"))
        assert result["table"] == "cmdb_ci"
        assert result["business_rules"]["total_count"] == 5
        assert result["business_rules"]["active_count"] == 5
        assert result["client_scripts"]["total_count"] == 5
        assert result["flows"]["total_count"] == 5
        assert result["acls"]["total_count"] == 5

    @pytest.mark.asyncio
    async def test_zero_counts(self, mock_client, tools):
        result = _parse(await tools["analyze_configurables"](table="cmdb_ci"))
        assert result["business_rules"]["total_count"] == 0
        assert result["acls"]["active_count"] == 0

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["analyze_configurables"](table="bad/table"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_empty_table(self, tools):
        result = _parse(await tools["analyze_configurables"](table=""))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_partial_permission_failure(self, mock_client, tools):
        """If some aggregate calls fail, those categories show error, others succeed."""
        from servicenow_cmdb_mcp.errors import SNPermissionError

        call_count = 0
        async def _selective_fail(**kwargs):
            nonlocal call_count
            call_count += 1
            # Fail on ACL calls (calls 7 and 8)
            if "sys_security_acl" in kwargs.get("table", ""):
                raise SNPermissionError("Denied")
            return {"result": {"stats": {"count": "3"}}}

        mock_client.get_aggregate.side_effect = _selective_fail
        result = _parse(await tools["analyze_configurables"](table="cmdb_ci"))
        # Accessible categories should have counts
        assert result["business_rules"]["total_count"] == 3
        assert result["client_scripts"]["active_count"] == 3
        assert result["flows"]["total_count"] == 3
        # Denied category should show error
        assert result["acls"]["error"] is not None
        assert result["acls"]["total_count"] is None

    @pytest.mark.asyncio
    async def test_all_categories_denied(self, mock_client, tools):
        """If all aggregate calls fail, all categories show error but no top-level crash."""
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_aggregate.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["analyze_configurables"](table="cmdb_ci"))
        # Should NOT have a top-level error — instead per-category errors
        assert "error" not in result
        assert result["table"] == "cmdb_ci"
        assert result["business_rules"]["total_count"] is None
        assert result["acls"]["total_count"] is None

    @pytest.mark.asyncio
    async def test_aggregate_calls_correct_queries(self, mock_client, tools):
        await tools["analyze_configurables"](table="cmdb_ci_server")
        calls = mock_client.get_aggregate.call_args_list
        assert len(calls) == 8  # 4 types x 2 (total + active)

        queries = [c.kwargs["query"] for c in calls]
        assert "collection=cmdb_ci_server" in queries
        assert "collection=cmdb_ci_server^active=true" in queries
        assert "table=cmdb_ci_server" in queries
        assert "table=cmdb_ci_server^active=true" in queries
        assert "internal_nameCONTAINScmdb_ci_server" in queries
        assert "internal_nameCONTAINScmdb_ci_server^active=true" in queries
        assert "nameSTARTSWITHcmdb_ci_server" in queries
        assert "nameSTARTSWITHcmdb_ci_server^active=true" in queries

    @pytest.mark.asyncio
    async def test_malformed_aggregate_response(self, mock_client, tools):
        """Gracefully handle unexpected aggregate response shapes."""
        mock_client.get_aggregate.return_value = {"result": "unexpected"}
        result = _parse(await tools["analyze_configurables"](table="cmdb_ci"))
        assert result["business_rules"]["total_count"] == 0
