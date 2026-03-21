"""Tests for tools/ire.py — IRE inspection tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.tools._utils import _validate_sys_id, _validate_table_name
from servicenow_cmdb_mcp.tools.ire import register_ire_tools


# ── Fake sys_ids ────────────────────────────────────────────────────

CI_A = "a" * 32
CI_B = "b" * 32


# ── Helpers ─────────────────────────────────────────────────────────


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


def _ci_record(sys_id: str, name: str = "Server-A", cls: str = "cmdb_ci_server"):
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": cls,
        "serial_number": "SN001",
        "ip_address": "10.0.1.1",
    }


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value=_ci_record(CI_A))
    client.get_aggregate = AsyncMock(return_value={"result": {"stats": {"count": "0"}}})
    return client


@pytest.fixture
def tools(mock_client):
    """Register IRE tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_ire_tools(mcp, mock_client)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Unit tests: helpers ─────────────────────────────────────────────


class TestValidateTableName:
    def test_valid(self):
        assert _validate_table_name("cmdb_ci_server") is None

    def test_empty(self):
        assert _validate_table_name("") is not None

    def test_special(self):
        assert _validate_table_name("bad/table") is not None


class TestValidateSysId:
    def test_valid(self):
        assert _validate_sys_id(CI_A) is None

    def test_empty(self):
        assert _validate_sys_id("") is not None

    def test_blank(self):
        assert _validate_sys_id("   ") is not None


# ── get_identification_rules ────────────────────────────────────────


class TestGetIdentificationRules:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_identification_rules"]())
        assert result["count"] == 0
        assert result["identification_rules"] == []
        assert result["table_filter"] == "(all)"

    @pytest.mark.asyncio
    async def test_pagination_signals(self, mock_client, tools):
        result = _parse(await tools["get_identification_rules"]())
        assert result["total_count"] == 0
        assert result["has_more"] is False
        assert result["next_offset"] == 0

    @pytest.mark.asyncio
    async def test_returns_rules(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "r1",
                "name": "Server ID Rule",
                "applies_to": "cmdb_ci_server",
                "active": "true",
                "identifiers": "serial_number,name",
                "priority": "1",
                "description": "Match by serial + name",
            }
        ]
        result = _parse(await tools["get_identification_rules"]())
        assert result["count"] == 1
        rule = result["identification_rules"][0]
        assert rule["name"] == "Server ID Rule"
        assert rule["identifiers"] == "serial_number,name"

    @pytest.mark.asyncio
    async def test_filter_by_table(self, mock_client, tools):
        await tools["get_identification_rules"](table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "applies_to=cmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_no_table_filter(self, mock_client, tools):
        await tools["get_identification_rules"]()
        call_args = mock_client.get_records.call_args
        assert "applies_to" not in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_active_filter(self, mock_client, tools):
        await tools["get_identification_rules"](active_only=True)
        call_args = mock_client.get_records.call_args
        assert "active=true" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_all_rules(self, mock_client, tools):
        await tools["get_identification_rules"](active_only=False)
        call_args = mock_client.get_records.call_args
        assert "active=true" not in call_args.kwargs.get("query", "")

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_identification_rules"](table="bad/table"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_limit_clamped(self, mock_client, tools):
        await tools["get_identification_rules"](limit=9999)
        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_identification_rules"]())
        assert result["error"] is True


# ── get_reconciliation_rules ────────────────────────────────────────


class TestGetReconciliationRules:
    @pytest.mark.asyncio
    async def test_returns_empty(self, mock_client, tools):
        result = _parse(await tools["get_reconciliation_rules"]())
        assert result["count"] == 0
        assert result["reconciliation_rules"] == []

    @pytest.mark.asyncio
    async def test_returns_rules(self, mock_client, tools):
        mock_client.get_records.return_value = [
            {
                "sys_id": "rr1",
                "name": "Discovery Wins",
                "applies_to": "cmdb_ci_server",
                "active": "true",
                "source": "ServiceNow Discovery",
                "priority": "1",
                "attributes": "ip_address,os",
                "description": "Discovery is authoritative for network fields",
            }
        ]
        result = _parse(await tools["get_reconciliation_rules"]())
        assert result["count"] == 1
        rule = result["reconciliation_rules"][0]
        assert rule["name"] == "Discovery Wins"
        assert rule["source"] == "ServiceNow Discovery"

    @pytest.mark.asyncio
    async def test_filter_by_table(self, mock_client, tools):
        await tools["get_reconciliation_rules"](table="cmdb_ci_server")
        call_args = mock_client.get_records.call_args
        assert "applies_to=cmdb_ci_server" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["get_reconciliation_rules"](table="bad;drop"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["get_reconciliation_rules"]())
        assert result["error"] is True


# ── explain_duplicate ───────────────────────────────────────────────


class TestExplainDuplicate:
    @pytest.mark.asyncio
    async def test_compares_two_cis(self, mock_client, tools):
        mock_client.get_record.side_effect = [
            _ci_record(CI_A, name="Server-A"),
            _ci_record(CI_B, name="Server-A"),
        ]
        mock_client.get_records.return_value = [
            {
                "sys_id": "r1",
                "name": "Name Rule",
                "applies_to": "cmdb_ci",
                "identifiers": "name",
                "priority": "1",
            }
        ]
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B, table="cmdb_ci",
        ))
        assert result["ci_a"]["sys_id"] == CI_A
        assert result["ci_b"]["sys_id"] == CI_B
        assert len(result["identification_rules"]) == 1
        assert result["identification_rules"][0]["identifier_fields"] == ["name"]

        # name field should match
        name_cmp = next(c for c in result["field_comparison"] if c["field"] == "name")
        assert name_cmp["match"] is True
        assert name_cmp["is_identifier"] is True

        assert result["summary"]["likely_duplicate"] is True

    @pytest.mark.asyncio
    async def test_not_duplicate(self, mock_client, tools):
        mock_client.get_record.side_effect = [
            _ci_record(CI_A, name="Server-A"),
            _ci_record(CI_B, name="Server-B"),
        ]
        mock_client.get_records.return_value = [
            {
                "sys_id": "r1",
                "name": "Name Rule",
                "applies_to": "cmdb_ci",
                "identifiers": "name",
                "priority": "1",
            }
        ]
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B, table="cmdb_ci",
        ))
        assert result["summary"]["likely_duplicate"] is False
        assert result["summary"]["mismatched_identifiers"] == 1

    @pytest.mark.asyncio
    async def test_no_ident_rules(self, mock_client, tools):
        """When no identification rules exist, still compare common fields."""
        mock_client.get_record.side_effect = [
            _ci_record(CI_A),
            _ci_record(CI_B),
        ]
        mock_client.get_records.return_value = []
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B,
        ))
        assert result["identification_rules"] == []
        assert len(result["field_comparison"]) > 0  # common fields still compared
        assert result["summary"]["total_identifier_fields"] == 0

    @pytest.mark.asyncio
    async def test_empty_sys_id_a(self, tools):
        result = _parse(await tools["explain_duplicate"](
            sys_id_a="", sys_id_b=CI_B,
        ))
        assert result["error"] is True
        assert "sys_id_a" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_sys_id_b(self, tools):
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b="",
        ))
        assert result["error"] is True
        assert "sys_id_b" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_table(self, tools):
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B, table="bad/table",
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_ci_a_not_found(self, mock_client, tools):
        mock_client.get_record.side_effect = [None, _ci_record(CI_B)]
        mock_client.get_records.return_value = []
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B,
        ))
        assert result["error"] is True
        assert result["category"] == "NotFoundError"
        assert "CI A" in result["message"]

    @pytest.mark.asyncio
    async def test_ci_b_not_found(self, mock_client, tools):
        mock_client.get_record.side_effect = [_ci_record(CI_A), None]
        mock_client.get_records.return_value = []
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B,
        ))
        assert result["error"] is True
        assert "CI B" in result["message"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        from servicenow_cmdb_mcp.errors import SNPermissionError
        mock_client.get_record.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B,
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_ire_table_missing_still_compares(self, mock_client, tools):
        """If cmdb_ident_entry doesn't exist, still compare common fields."""
        from servicenow_cmdb_mcp.errors import SNValidationError

        call_count = 0
        original_get_records = mock_client.get_records

        async def _fail_on_ident(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("table") == "cmdb_ident_entry":
                raise SNValidationError("Invalid table cmdb_ident_entry")
            return await original_get_records(**kwargs)

        mock_client.get_records.side_effect = _fail_on_ident
        mock_client.get_record.side_effect = [
            _ci_record(CI_A, name="Server-A"),
            _ci_record(CI_B, name="Server-A"),
        ]
        result = _parse(await tools["explain_duplicate"](
            sys_id_a=CI_A, sys_id_b=CI_B,
        ))
        # Should succeed with no ident rules but still compare common fields
        assert "error" not in result
        assert result["identification_rules"] == []
        assert len(result["field_comparison"]) > 0

    @pytest.mark.asyncio
    async def test_parallel_fetch(self, mock_client, tools):
        """Both CIs and ident rules should be fetched in parallel."""
        mock_client.get_record.side_effect = [
            _ci_record(CI_A), _ci_record(CI_B),
        ]
        mock_client.get_records.return_value = []
        await tools["explain_duplicate"](sys_id_a=CI_A, sys_id_b=CI_B)
        # get_record called twice, get_records called once — all via gather
        assert mock_client.get_record.call_count == 2
        assert mock_client.get_records.call_count == 1
