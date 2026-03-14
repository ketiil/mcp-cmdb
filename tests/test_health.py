"""Tests for tools/health.py — orphan, duplicate, stale, and health summary tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.errors import SNPermissionError
from servicenow_cmdb_mcp.tools.health import (
    _clamp_limit,
    _clamp_offset,
    _extract_count,
    _parse_agg_groups,
    register_health_tools,
)

# ── Fake sys_ids ────────────────────────────────────────────────────

CI_A = "a" * 32
CI_B = "b" * 32
CI_C = "c" * 32


# ── Helpers ─────────────────────────────────────────────────────────

def _ci_record(sys_id: str, name: str, cls: str = "cmdb_ci_server", status: str = "1"):
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": cls,
        "operational_status": status,
        "sys_updated_on": "2025-01-01 00:00:00",
        "discovery_source": "ServiceNow",
    }


def _agg_response(count: int) -> dict:
    return {"result": {"stats": {"count": str(count)}}}


def _agg_grouped(groups: list[tuple[str, int]], field: str = "operational_status") -> dict:
    return {
        "result": [
            {
                "stats": {"count": str(count)},
                "groupby_fields": [{"field": field, "value": value}],
            }
            for value, count in groups
        ]
    }


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value=None)
    client.get_aggregate = AsyncMock(return_value=_agg_response(0))
    return client


@pytest.fixture
def tools(mock_client):
    """Register health tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_health_tools(mcp, mock_client)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Unit tests: helpers ─────────────────────────────────────────────

class TestExtractCount:
    def test_normal_response(self):
        assert _extract_count({"result": {"stats": {"count": "42"}}}) == 42

    def test_zero(self):
        assert _extract_count({"result": {"stats": {"count": "0"}}}) == 0

    def test_missing_stats(self):
        assert _extract_count({"result": {}}) == 0

    def test_missing_result(self):
        assert _extract_count({}) == 0

    def test_non_numeric_count(self):
        assert _extract_count({"result": {"stats": {"count": "not-a-number"}}}) == 0

    def test_none_count(self):
        assert _extract_count({"result": {"stats": {"count": None}}}) == 0


class TestParseAggGroups:
    def test_normal_groups(self):
        agg = _agg_grouped([("1", 800), ("2", 200)])
        result = _parse_agg_groups(agg)
        assert result == {"1": 800, "2": 200}

    def test_empty_result(self):
        assert _parse_agg_groups({"result": []}) == {}

    def test_dict_result_wrapped(self):
        agg = {"result": {"stats": {"count": "5"}, "groupby_fields": [{"value": "x"}]}}
        result = _parse_agg_groups(agg)
        assert result == {"x": 5}

    def test_empty_value_uses_label(self):
        agg = _agg_grouped([("", 10)])
        result = _parse_agg_groups(agg, empty_label="(none)")
        assert result == {"(none)": 10}

    def test_non_numeric_count_skipped(self):
        agg = {"result": [{"stats": {"count": "bad"}, "groupby_fields": [{"value": "x"}]}]}
        result = _parse_agg_groups(agg)
        assert result == {"x": 0}


class TestClampHelpers:
    def test_limit_boundaries(self):
        assert _clamp_limit(0) == 1
        assert _clamp_limit(-1) == 1
        assert _clamp_limit(5000) == 1000
        assert _clamp_limit(50) == 50

    def test_offset_boundaries(self):
        assert _clamp_offset(-1) == 0
        assert _clamp_offset(0) == 0
        assert _clamp_offset(100) == 100


# ── find_orphan_cis ─────────────────────────────────────────────────

class TestFindOrphanCis:
    @pytest.mark.asyncio
    async def test_finds_orphans(self, mock_client, tools):
        """CIs with 0 relationships should be returned as orphans."""
        mock_client.get_records.return_value = [
            _ci_record(CI_A, "Orphan-A"),
            _ci_record(CI_B, "Linked-B"),
        ]
        # CI_A has 0 rels, CI_B has 2 rels
        mock_client.get_aggregate.side_effect = [
            _agg_response(0),  # CI_A
            _agg_response(2),  # CI_B
        ]

        result = _parse(await tools["find_orphan_cis"]())
        assert result["count"] == 1
        assert result["orphan_cis"][0]["sys_id"] == CI_A

    @pytest.mark.asyncio
    async def test_no_orphans(self, mock_client, tools):
        """All CIs have relationships — should return empty list."""
        mock_client.get_records.return_value = [_ci_record(CI_A, "Linked")]
        mock_client.get_aggregate.return_value = _agg_response(3)

        result = _parse(await tools["find_orphan_cis"]())
        assert result["count"] == 0
        assert result["orphan_cis"] == []

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_client, tools):
        """Should stop after collecting `limit` orphans."""
        mock_client.get_records.return_value = [
            _ci_record(f"{'a' * 31}{i}", f"Orphan-{i}") for i in range(10)
        ]
        mock_client.get_aggregate.return_value = _agg_response(0)  # All orphans

        result = _parse(await tools["find_orphan_cis"](limit=3))
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_filters_by_class(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["find_orphan_cis"](ci_class="cmdb_ci_server")

        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["table"] == "cmdb_ci_server"

    @pytest.mark.asyncio
    async def test_filters_by_operational_status(self, mock_client, tools):
        mock_client.get_records.return_value = []
        await tools["find_orphan_cis"](operational_status="1")

        call_args = mock_client.get_records.call_args
        assert "operational_status=1" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_skips_empty_sys_id(self, mock_client, tools):
        """Records with empty sys_id should be skipped."""
        mock_client.get_records.return_value = [
            {"sys_id": "", "name": "Bad"},
            _ci_record(CI_A, "Good"),
        ]
        mock_client.get_aggregate.return_value = _agg_response(0)

        result = _parse(await tools["find_orphan_cis"]())
        assert result["count"] == 1
        assert result["orphan_cis"][0]["sys_id"] == CI_A

    @pytest.mark.asyncio
    async def test_returns_scan_metadata(self, mock_client, tools):
        """Response should include scanning progress info."""
        mock_client.get_records.return_value = [
            _ci_record(CI_A, "Orphan-A"),
            _ci_record(CI_B, "Linked-B"),
        ]
        mock_client.get_aggregate.side_effect = [
            _agg_response(0),  # CI_A orphan
            _agg_response(1),  # CI_B linked
        ]

        result = _parse(await tools["find_orphan_cis"]())
        assert result["total_scanned"] == 2
        assert result["next_scan_offset"] == 2
        assert "may_have_more" in result

    @pytest.mark.asyncio
    async def test_scan_offset_continues(self, mock_client, tools):
        """scan_offset should be passed to the CI fetch."""
        mock_client.get_records.return_value = []
        await tools["find_orphan_cis"](scan_offset=100)

        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["offset"] == 100

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["find_orphan_cis"]())
        assert result["error"] is True


# ── find_duplicate_cis ──────────────────────────────────────────────

class TestFindDuplicateCis:
    @pytest.mark.asyncio
    async def test_invalid_match_field(self, tools):
        result = _parse(await tools["find_duplicate_cis"](match_field="sys_script"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_finds_duplicates(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_grouped(
            [("web-server-01", 3), ("db-server-01", 2), ("unique-ci", 1)],
            field="name",
        )
        mock_client.get_records.return_value = [
            _ci_record(CI_A, "web-server-01"),
            _ci_record(CI_B, "web-server-01"),
        ]

        result = _parse(await tools["find_duplicate_cis"]())
        # Only groups with count > 1 should be returned
        assert result["duplicate_group_count"] == 2
        assert result["duplicate_groups"][0]["value"] == "web-server-01"
        assert result["duplicate_groups"][0]["total_count"] == 3

    @pytest.mark.asyncio
    async def test_no_duplicates(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_grouped(
            [("unique-a", 1), ("unique-b", 1)],
            field="name",
        )

        result = _parse(await tools["find_duplicate_cis"]())
        assert result["duplicate_group_count"] == 0

    @pytest.mark.asyncio
    async def test_respects_match_field(self, mock_client, tools):
        mock_client.get_aggregate.return_value = {"result": []}
        await tools["find_duplicate_cis"](match_field="serial_number")

        call_args = mock_client.get_aggregate.call_args
        assert call_args.kwargs["group_by"] == "serial_number"
        assert "serial_numberISNOTEMPTY" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_name_filter(self, mock_client, tools):
        mock_client.get_aggregate.return_value = {"result": []}
        await tools["find_duplicate_cis"](name_filter="web")

        call_args = mock_client.get_aggregate.call_args
        assert "nameSTARTSWITHweb" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_skips_values_with_query_operators(self, mock_client, tools):
        """Values containing ^ should be skipped to prevent query injection."""
        mock_client.get_aggregate.return_value = _agg_grouped(
            [("safe-name", 2), ("bad^ORactive=true", 3)],
            field="name",
        )
        mock_client.get_records.return_value = [_ci_record(CI_A, "safe-name")]

        result = _parse(await tools["find_duplicate_cis"]())
        # Only the safe group should have records fetched
        assert result["duplicate_group_count"] == 1
        assert result["duplicate_groups"][0]["value"] == "safe-name"

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_aggregate.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["find_duplicate_cis"]())
        assert result["error"] is True


# ── find_stale_cis ──────────────────────────────────────────────────

class TestFindStaleCis:
    @pytest.mark.asyncio
    async def test_finds_stale_cis(self, mock_client, tools):
        stale_records = [_ci_record(CI_A, "Stale-A"), _ci_record(CI_B, "Stale-B")]
        mock_client.get_records.return_value = stale_records
        mock_client.get_aggregate.return_value = _agg_response(50)

        result = _parse(await tools["find_stale_cis"](days=30))
        assert result["count"] == 2
        assert result["total_stale"] == 50
        assert result["stale_days"] == 30

    @pytest.mark.asyncio
    async def test_uses_days_ago_query(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["find_stale_cis"](days=60)

        call_args = mock_client.get_records.call_args
        assert "gs.daysAgo(60)" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_filters_operational_status(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["find_stale_cis"](operational_status="1")

        call_args = mock_client.get_records.call_args
        assert "operational_status=1" in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_empty_status_includes_all(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["find_stale_cis"](operational_status="")

        call_args = mock_client.get_records.call_args
        assert "operational_status" not in call_args.kwargs["query"]

    @pytest.mark.asyncio
    async def test_days_clamped(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_aggregate.return_value = _agg_response(0)

        result = _parse(await tools["find_stale_cis"](days=0))
        assert result["stale_days"] == 1

        result = _parse(await tools["find_stale_cis"](days=99999))
        assert result["stale_days"] == 3650

    @pytest.mark.asyncio
    async def test_orders_by_sys_updated_on(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["find_stale_cis"]()

        call_args = mock_client.get_records.call_args
        assert call_args.kwargs["order_by"] == "ORDERBYsys_updated_on"

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["find_stale_cis"]())
        assert result["error"] is True


# ── cmdb_health_summary ─────────────────────────────────────────────

class TestCmdbHealthSummary:
    @pytest.mark.asyncio
    async def test_returns_all_metrics(self, mock_client, tools):
        mock_client.get_aggregate.side_effect = [
            _agg_response(1000),       # total
            _agg_grouped([("1", 800), ("2", 200)]),  # by status
            _agg_response(150),         # stale
            _agg_response(5),           # missing name
            _agg_grouped([("ServiceNow", 600), ("", 400)]),  # by source
        ]

        result = _parse(await tools["cmdb_health_summary"]())
        assert result["total_count"] == 1000
        assert result["by_operational_status"]["1"] == 800
        assert result["by_operational_status"]["2"] == 200
        assert result["stale_count"] == 150
        assert result["missing_name_count"] == 5
        assert result["by_discovery_source"]["ServiceNow"] == 600

    @pytest.mark.asyncio
    async def test_uses_correct_table(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["cmdb_health_summary"](ci_class="cmdb_ci_server")

        # All aggregate calls should use the specified table
        for call in mock_client.get_aggregate.call_args_list:
            assert call.kwargs["table"] == "cmdb_ci_server"

    @pytest.mark.asyncio
    async def test_stale_days_clamped(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_response(0)

        result = _parse(await tools["cmdb_health_summary"](stale_days=0))
        assert result["stale_days"] == 1

    @pytest.mark.asyncio
    async def test_stale_days_in_query(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["cmdb_health_summary"](stale_days=60)

        # The third aggregate call should be the stale query
        stale_call = mock_client.get_aggregate.call_args_list[2]
        assert "gs.daysAgo(60)" in stale_call.kwargs["query"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_aggregate.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["cmdb_health_summary"]())
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_makes_five_aggregate_calls(self, mock_client, tools):
        mock_client.get_aggregate.return_value = _agg_response(0)

        await tools["cmdb_health_summary"]()
        assert mock_client.get_aggregate.call_count == 5
