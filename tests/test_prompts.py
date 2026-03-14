"""Tests for prompts/workflows.py — MCP Prompt definitions."""

from __future__ import annotations

import pytest

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.prompts.workflows import register_prompts


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mcp_app() -> FastMCP:
    mcp = FastMCP("test")
    register_prompts(mcp)
    return mcp


@pytest.fixture
def prompts(mcp_app) -> dict:
    """Return prompt objects keyed by name."""
    return {
        name: prompt
        for name, prompt in mcp_app._prompt_manager._prompts.items()
    }


# ── Registration ────────────────────────────────────────────────────


class TestPromptRegistration:
    def test_registers_all_prompts(self, prompts):
        assert "health_check" in prompts
        assert "impact_analysis" in prompts
        assert "troubleshoot_ci" in prompts
        assert "audit_configurables" in prompts

    def test_prompt_count(self, prompts):
        assert len(prompts) == 4


# ── Health Check ────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_default_class(self, prompts):
        result = await prompts["health_check"].render({})
        text = result[0].content.text
        assert "cmdb_ci" in text
        assert "cmdb_health_summary" in text
        assert "find_orphan_cis" in text
        assert "find_duplicate_cis" in text
        assert "find_stale_cis" in text

    @pytest.mark.asyncio
    async def test_custom_class(self, prompts):
        result = await prompts["health_check"].render({"ci_class": "cmdb_ci_server"})
        text = result[0].content.text
        assert "cmdb_ci_server" in text

    @pytest.mark.asyncio
    async def test_has_arguments(self, prompts):
        args = prompts["health_check"].arguments
        arg_names = [a.name for a in args]
        assert "ci_class" in arg_names

    @pytest.mark.asyncio
    async def test_ci_class_optional(self, prompts):
        args = prompts["health_check"].arguments
        ci_class_arg = next(a for a in args if a.name == "ci_class")
        assert ci_class_arg.required is False


# ── Impact Analysis ─────────────────────────────────────────────────


class TestImpactAnalysis:
    @pytest.mark.asyncio
    async def test_renders_with_identifier(self, prompts):
        result = await prompts["impact_analysis"].render({"ci_identifier": "web-server-01"})
        text = result[0].content.text
        assert "web-server-01" in text
        assert "get_ci_details" in text
        assert "get_dependency_tree" in text
        assert "get_ci_relationships" in text

    @pytest.mark.asyncio
    async def test_default_depth(self, prompts):
        result = await prompts["impact_analysis"].render({"ci_identifier": "test"})
        text = result[0].content.text
        assert "depth=3" in text

    @pytest.mark.asyncio
    async def test_custom_depth(self, prompts):
        result = await prompts["impact_analysis"].render({"ci_identifier": "test", "depth": "5"})
        text = result[0].content.text
        assert "depth=5" in text

    @pytest.mark.asyncio
    async def test_ci_identifier_required(self, prompts):
        args = prompts["impact_analysis"].arguments
        ci_arg = next(a for a in args if a.name == "ci_identifier")
        assert ci_arg.required is True


# ── Troubleshoot CI ─────────────────────────────────────────────────


class TestTroubleshootCi:
    @pytest.mark.asyncio
    async def test_renders_with_identifier(self, prompts):
        result = await prompts["troubleshoot_ci"].render({"ci_identifier": "db-server-03"})
        text = result[0].content.text
        assert "db-server-03" in text
        assert "get_ci_details" in text
        assert "get_ci_relationships" in text
        assert "analyze_configurables" in text
        assert "get_identification_rules" in text

    @pytest.mark.asyncio
    async def test_ci_identifier_required(self, prompts):
        args = prompts["troubleshoot_ci"].arguments
        ci_arg = next(a for a in args if a.name == "ci_identifier")
        assert ci_arg.required is True


# ── Audit Configurables ────────────────────────────────────────────


class TestAuditConfigurables:
    @pytest.mark.asyncio
    async def test_renders_with_table(self, prompts):
        result = await prompts["audit_configurables"].render({"table": "cmdb_ci_server"})
        text = result[0].content.text
        assert "cmdb_ci_server" in text
        assert "get_business_rules" in text
        assert "get_flows" in text
        assert "get_client_scripts" in text
        assert "get_acls" in text

    @pytest.mark.asyncio
    async def test_table_required(self, prompts):
        args = prompts["audit_configurables"].arguments
        table_arg = next(a for a in args if a.name == "table")
        assert table_arg.required is True

    @pytest.mark.asyncio
    async def test_mentions_conflict_analysis(self, prompts):
        result = await prompts["audit_configurables"].render({"table": "cmdb_ci"})
        text = result[0].content.text
        assert "conflict" in text.lower()
