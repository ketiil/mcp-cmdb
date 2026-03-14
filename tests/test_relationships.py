"""Tests for tools/relationships.py — relationship and dependency tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.errors import NotFoundError, SNPermissionError
from servicenow_cmdb_mcp.tools.relationships import (
    _clamp_limit,
    _clamp_offset,
    _validate_sys_id,
    register_relationship_tools,
)

# ── Fake sys_ids used across tests ──────────────────────────────────

CI_A = "a" * 32
CI_B = "b" * 32
CI_C = "c" * 32
REL_TYPE_ID = "d" * 32
REL_SYS_ID = "e" * 32


# ── Helpers ─────────────────────────────────────────────────────────

def _ci_record(sys_id: str, name: str, cls: str = "cmdb_ci_server", status: str = "1"):
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": cls,
        "operational_status": status,
    }


def _rel_record(parent: str, child: str, type_id: str = REL_TYPE_ID, sys_id: str = REL_SYS_ID):
    return {
        "sys_id": sys_id,
        "parent": {"link": "", "value": parent},
        "child": {"link": "", "value": child},
        "type": {"link": "", "value": type_id},
    }


def _rel_type_record(sys_id: str = REL_TYPE_ID, name: str = "Runs on::Runs"):
    return {
        "sys_id": sys_id,
        "name": name,
        "parent_descriptor": "Runs on",
        "child_descriptor": "Runs",
    }


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def cache() -> MetadataCache:
    return MetadataCache(ttl=3600)


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_records = AsyncMock(return_value=[])
    client.get_record = AsyncMock(return_value=None)
    return client


@pytest.fixture
def tools(mock_client, cache):
    """Register relationship tools on a FastMCP instance and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_relationship_tools(mcp, mock_client, cache)

    # Extract the registered tool functions by name
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Unit tests: helper functions ────────────────────────────────────

class TestClampLimit:
    def test_normal_value(self):
        assert _clamp_limit(25) == 25

    def test_zero(self):
        assert _clamp_limit(0) == 1

    def test_negative(self):
        assert _clamp_limit(-10) == 1

    def test_above_max(self):
        assert _clamp_limit(5000) == 1000

    def test_boundary(self):
        assert _clamp_limit(1000) == 1000
        assert _clamp_limit(1) == 1


class TestClampOffset:
    def test_normal_value(self):
        assert _clamp_offset(10) == 10

    def test_zero(self):
        assert _clamp_offset(0) == 0

    def test_negative(self):
        assert _clamp_offset(-5) == 0


class TestValidateSysId:
    def test_valid(self):
        assert _validate_sys_id("abc123") is None

    def test_valid_32_hex(self):
        assert _validate_sys_id("a" * 32) is None

    def test_empty(self):
        assert _validate_sys_id("") is not None

    def test_blank(self):
        assert _validate_sys_id("   ") is not None

    def test_path_traversal(self):
        assert _validate_sys_id("../../etc/passwd") is not None

    def test_special_chars(self):
        assert _validate_sys_id("abc-123") is not None


# ── get_ci_relationships ────────────────────────────────────────────

class TestGetCiRelationships:
    @pytest.mark.asyncio
    async def test_empty_sys_id_returns_validation_error(self, tools):
        result = _parse(await tools["get_ci_relationships"](""))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_invalid_direction_returns_validation_error(self, tools):
        result = _parse(await tools["get_ci_relationships"](CI_A, direction="sideways"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"
        assert "sideways" in result["message"]

    @pytest.mark.asyncio
    async def test_upstream_queries_child_field(self, mock_client, tools):
        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci":
                return [_rel_record(parent=CI_B, child=CI_A)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [_ci_record(CI_B, "Server-B")]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.return_value = _rel_type_record()

        result = _parse(await tools["get_ci_relationships"](CI_A, direction="upstream"))

        assert result["count"] == 1
        assert result["relationships"][0]["direction"] == "upstream"
        assert result["relationships"][0]["related_ci"]["sys_id"] == CI_B

        # Verify the first query was for child=CI_A (upstream means CI is child)
        first_call = mock_client.get_records.call_args_list[0]
        assert f"child={CI_A}" in first_call.kwargs["query"]

    @pytest.mark.asyncio
    async def test_downstream_queries_parent_field(self, mock_client, tools):
        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci":
                return [_rel_record(parent=CI_A, child=CI_B)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [_ci_record(CI_B, "Server-B")]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.return_value = _rel_type_record()

        result = _parse(await tools["get_ci_relationships"](CI_A, direction="downstream"))

        assert result["count"] == 1
        assert result["relationships"][0]["direction"] == "downstream"
        assert result["relationships"][0]["related_ci"]["sys_id"] == CI_B

        first_call = mock_client.get_records.call_args_list[0]
        assert f"parent={CI_A}" in first_call.kwargs["query"]

    @pytest.mark.asyncio
    async def test_both_direction_splits_limit(self, mock_client, tools):
        mock_client.get_records.return_value = []

        await tools["get_ci_relationships"](CI_A, direction="both", limit=20)

        # Should make 2 rel queries (upstream + downstream), each with limit=10
        rel_calls = [c for c in mock_client.get_records.call_args_list
                     if c.kwargs.get("table") == "cmdb_rel_ci"]
        assert len(rel_calls) == 2
        for call in rel_calls:
            assert call.kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_no_relationships_returns_empty(self, mock_client, tools):
        mock_client.get_records.return_value = []
        result = _parse(await tools["get_ci_relationships"](CI_A))
        assert result["count"] == 0
        assert result["relationships"] == []

    @pytest.mark.asyncio
    async def test_service_now_error_returns_structured_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Access denied")
        result = _parse(await tools["get_ci_relationships"](CI_A))
        assert result["error"] is True
        assert result["category"] == "PermissionError"


# ── get_dependency_tree ─────────────────────────────────────────────

class TestGetDependencyTree:
    @pytest.mark.asyncio
    async def test_empty_sys_id_returns_validation_error(self, tools):
        result = _parse(await tools["get_dependency_tree"](""))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_invalid_direction(self, tools):
        result = _parse(await tools["get_dependency_tree"](CI_A, direction="both"))
        assert result["error"] is True
        assert "upstream" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_max_depth_clamped_to_5(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_record.return_value = _ci_record(CI_A, "Server-A")

        result = _parse(await tools["get_dependency_tree"](CI_A, max_depth=99))
        assert result["max_depth"] == 5

    @pytest.mark.asyncio
    async def test_max_depth_clamped_to_1(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_record.return_value = _ci_record(CI_A, "Server-A")

        result = _parse(await tools["get_dependency_tree"](CI_A, max_depth=0))
        assert result["max_depth"] == 1

    @pytest.mark.asyncio
    async def test_single_level_tree(self, mock_client, tools):
        # Root CI_A has one upstream dependency CI_B
        ci_lookup = {
            CI_A: _ci_record(CI_A, "Server-A"),
            CI_B: _ci_record(CI_B, "Server-B"),
        }

        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci" and f"child={CI_A}" in query:
                return [_rel_record(parent=CI_B, child=CI_A)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id"))
            or (_rel_type_record() if kw.get("table") == "cmdb_rel_type" else None)
        )

        result = _parse(await tools["get_dependency_tree"](CI_A, max_depth=2))
        tree = result["tree"]
        assert tree["ci"]["sys_id"] == CI_A
        assert len(tree["children"]) == 1
        assert tree["children"][0]["ci"]["sys_id"] == CI_B

    @pytest.mark.asyncio
    async def test_cycle_detection(self, mock_client, tools):
        """A->B->A should not loop forever."""
        call_count = 0
        ci_lookup = {
            CI_A: _ci_record(CI_A, "Server-A"),
            CI_B: _ci_record(CI_B, "Server-B"),
        }

        def mock_get_records(**kwargs):
            nonlocal call_count
            call_count += 1
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci":
                if f"child={CI_A}" in query:
                    return [_rel_record(parent=CI_B, child=CI_A)]
                if f"child={CI_B}" in query:
                    return [_rel_record(parent=CI_A, child=CI_B)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id")) or _rel_type_record()
        )

        result = _parse(await tools["get_dependency_tree"](CI_A, max_depth=5))
        # Should complete without infinite loop
        assert result["tree"]["ci"]["sys_id"] == CI_A
        # Calls should be bounded
        assert call_count < 20  # Sanity: not spinning


# ── list_relationship_types ─────────────────────────────────────────

class TestListRelationshipTypes:
    @pytest.mark.asyncio
    async def test_returns_types(self, mock_client, tools):
        mock_client.get_records.return_value = [
            _rel_type_record(sys_id="type1", name="Runs on::Runs"),
            _rel_type_record(sys_id="type2", name="Depends on::Used by"),
        ]

        result = _parse(await tools["list_relationship_types"]())
        assert result["count"] == 2
        assert result["cached"] is False
        assert result["relationship_types"][0]["name"] == "Runs on::Runs"

    @pytest.mark.asyncio
    async def test_caches_results(self, mock_client, cache, tools):
        mock_client.get_records.return_value = [_rel_type_record()]

        # First call — fetches
        result1 = _parse(await tools["list_relationship_types"]())
        assert result1["cached"] is False

        # Second call — from cache
        result2 = _parse(await tools["list_relationship_types"]())
        assert result2["cached"] is True
        assert mock_client.get_records.call_count == 1  # Only one API call

    @pytest.mark.asyncio
    async def test_limit_slices_results(self, mock_client, tools):
        mock_client.get_records.return_value = [
            _rel_type_record(sys_id=f"type{i}", name=f"Type {i}")
            for i in range(10)
        ]
        result = _parse(await tools["list_relationship_types"](limit=3))
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_records.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["list_relationship_types"]())
        assert result["error"] is True


# ── find_related_cis ────────────────────────────────────────────────

class TestFindRelatedCis:
    @pytest.mark.asyncio
    async def test_empty_sys_id(self, tools):
        result = _parse(await tools["find_related_cis"]("", rel_type=REL_TYPE_ID))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_invalid_direction(self, tools):
        result = _parse(await tools["find_related_cis"](CI_A, rel_type=REL_TYPE_ID, direction="left"))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_query_injection_blocked(self, tools):
        result = _parse(await tools["find_related_cis"](
            CI_A, rel_type="foo^ORactive=true",
        ))
        assert result["error"] is True
        assert result["category"] == "ValidationError"
        assert "invalid characters" in result["message"]

    @pytest.mark.asyncio
    async def test_resolves_type_name(self, mock_client, tools):
        # First call resolves the name, then fetches relationships
        mock_client.get_records.side_effect = [
            [{"sys_id": REL_TYPE_ID, "name": "Runs on::Runs"}],  # name lookup
            [],  # upstream rel query
            [],  # downstream rel query
        ]

        result = _parse(await tools["find_related_cis"](
            CI_A, rel_type="Runs on::Runs",
        ))
        assert result["count"] == 0

        # Verify name lookup was made
        first_call = mock_client.get_records.call_args_list[0]
        assert first_call.kwargs["table"] == "cmdb_rel_type"
        assert "Runs on::Runs" in first_call.kwargs["query"]

    @pytest.mark.asyncio
    async def test_type_name_not_found(self, mock_client, tools):
        mock_client.get_records.return_value = []  # Name lookup returns nothing
        result = _parse(await tools["find_related_cis"](
            CI_A, rel_type="Nonexistent Type",
        ))
        assert result["error"] is True
        assert result["category"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_sys_id_type_skips_lookup(self, mock_client, tools):
        mock_client.get_records.return_value = []

        await tools["find_related_cis"](CI_A, rel_type=REL_TYPE_ID)

        # Should NOT look up the type name — cmdb_rel_type should not appear
        for call in mock_client.get_records.call_args_list:
            assert call.kwargs["table"] != "cmdb_rel_type"

    @pytest.mark.asyncio
    async def test_filters_by_rel_type(self, mock_client, tools):
        mock_client.get_records.return_value = []

        await tools["find_related_cis"](CI_A, rel_type=REL_TYPE_ID, direction="upstream")

        rel_calls = [c for c in mock_client.get_records.call_args_list
                     if c.kwargs.get("table") == "cmdb_rel_ci"]
        assert len(rel_calls) >= 1
        assert f"type={REL_TYPE_ID}" in rel_calls[0].kwargs["query"]


# ── get_impact_summary ──────────────────────────────────────────────

class TestGetImpactSummary:
    @pytest.mark.asyncio
    async def test_empty_sys_id(self, tools):
        result = _parse(await tools["get_impact_summary"](""))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_no_downstream_deps(self, mock_client, tools):
        mock_client.get_records.return_value = []
        mock_client.get_record.return_value = _ci_record(CI_A, "Server-A")

        result = _parse(await tools["get_impact_summary"](CI_A))
        assert result["total_impacted"] == 0
        assert result["impacted_services"] == []
        assert result["ci"]["sys_id"] == CI_A

    @pytest.mark.asyncio
    async def test_counts_impacted_by_class(self, mock_client, tools):
        ci_lookup = {
            CI_A: _ci_record(CI_A, "Server-A"),
            CI_B: _ci_record(CI_B, "App-B", cls="cmdb_ci_appl"),
            CI_C: _ci_record(CI_C, "Server-C", cls="cmdb_ci_server"),
        }

        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci" and f"parent={CI_A}" in query:
                return [
                    _rel_record(parent=CI_A, child=CI_B, sys_id="r1"),
                    _rel_record(parent=CI_A, child=CI_C, sys_id="r2"),
                ]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id")) or _rel_type_record()
        )

        result = _parse(await tools["get_impact_summary"](CI_A))
        assert result["total_impacted"] == 2
        assert result["impacted_by_class"]["cmdb_ci_appl"] == 1
        assert result["impacted_by_class"]["cmdb_ci_server"] == 1

    @pytest.mark.asyncio
    async def test_identifies_impacted_services(self, mock_client, tools):
        ci_lookup = {
            CI_A: _ci_record(CI_A, "Server-A"),
            CI_B: _ci_record(CI_B, "My Service", cls="cmdb_ci_service"),
        }

        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci" and f"parent={CI_A}" in query:
                return [_rel_record(parent=CI_A, child=CI_B)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id")) or _rel_type_record()
        )

        result = _parse(await tools["get_impact_summary"](CI_A))
        assert len(result["impacted_services"]) == 1
        assert result["impacted_services"][0]["name"] == "My Service"

    @pytest.mark.asyncio
    async def test_root_ci_excluded_from_impact(self, mock_client, tools):
        """Circular dep A->B->A should not list A as impacted by itself."""
        ci_lookup = {
            CI_A: _ci_record(CI_A, "Server-A"),
            CI_B: _ci_record(CI_B, "Server-B"),
        }

        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci":
                if f"parent={CI_A}" in query:
                    return [_rel_record(parent=CI_A, child=CI_B)]
                if f"parent={CI_B}" in query:
                    return [_rel_record(parent=CI_B, child=CI_A)]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id")) or _rel_type_record()
        )

        result = _parse(await tools["get_impact_summary"](CI_A))
        # Re-check: total_impacted should be 1 (only B), not 2
        assert result["total_impacted"] == 1

    @pytest.mark.asyncio
    async def test_no_duplicates_diamond(self, mock_client, tools):
        """Diamond: A->B, A->C, B->D, C->D — D should be counted once."""
        d_id = "f" * 32
        ci_lookup = {
            CI_A: _ci_record(CI_A, "A"),
            CI_B: _ci_record(CI_B, "B"),
            CI_C: _ci_record(CI_C, "C"),
            d_id: _ci_record(d_id, "D"),
        }

        def mock_get_records(**kwargs):
            table = kwargs.get("table", "")
            query = kwargs.get("query", "")
            if table == "cmdb_rel_ci":
                if f"parent={CI_A}" in query:
                    return [
                        _rel_record(parent=CI_A, child=CI_B, sys_id="r1"),
                        _rel_record(parent=CI_A, child=CI_C, sys_id="r2"),
                    ]
                if f"parent={CI_B}" in query:
                    return [_rel_record(parent=CI_B, child=d_id, sys_id="r3")]
                if f"parent={CI_C}" in query:
                    return [_rel_record(parent=CI_C, child=d_id, sys_id="r4")]
            if table == "cmdb_ci" and "sys_idIN" in query:
                return [v for k, v in ci_lookup.items() if k in query]
            return []

        mock_client.get_records.side_effect = mock_get_records
        mock_client.get_record.side_effect = lambda **kw: (
            ci_lookup.get(kw.get("sys_id")) or _rel_type_record()
        )

        result = _parse(await tools["get_impact_summary"](CI_A, max_depth=3))
        # B, C, D — each counted exactly once
        assert result["total_impacted"] == 3
