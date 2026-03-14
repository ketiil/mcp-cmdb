"""MCP Prompt definitions — reusable multi-step CMDB workflows."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register all MCP Prompts on the server."""

    @mcp.prompt(
        name="health_check",
        title="CMDB Health Check",
        description=(
            "Run a comprehensive CMDB health check for a CI class. "
            "Produces a structured report with orphan, duplicate, and stale CI counts "
            "plus actionable recommendations."
        ),
    )
    async def health_check(ci_class: str = "cmdb_ci") -> str:
        """CMDB Health Check workflow.

        Args:
            ci_class: The CMDB class to check (e.g. cmdb_ci_server). Defaults to cmdb_ci.
        """
        return (
            f"Run a full CMDB health check on the **{ci_class}** class. "
            "Follow these steps in order:\n\n"
            f"1. Call `cmdb_health_summary` for table `{ci_class}` to get overall metrics.\n"
            f"2. Call `find_orphan_cis` for class `{ci_class}` with limit 10 to find CIs with no relationships.\n"
            f"3. Call `find_duplicate_cis` for class `{ci_class}` with limit 10 to find potential duplicates.\n"
            f"4. Call `find_stale_cis` for class `{ci_class}` with limit 10 to find CIs not updated recently.\n\n"
            "After gathering all results, produce a structured health report with:\n"
            "- Summary metrics (total CIs, orphan count, duplicate count, stale count)\n"
            "- Top orphan, duplicate, and stale CIs with their details\n"
            "- Actionable recommendations for improving CMDB data quality\n"
            "- An overall health rating (Good / Needs Attention / Critical)"
        )

    @mcp.prompt(
        name="impact_analysis",
        title="Impact Analysis",
        description=(
            "Analyze the downstream impact of a CI. Shows all services, applications, "
            "and infrastructure that would be affected if this CI goes down."
        ),
    )
    async def impact_analysis(ci_identifier: str, depth: int = 3) -> str:
        """Impact Analysis workflow.

        Args:
            ci_identifier: CI name or sys_id to analyze.
            depth: How many relationship levels to traverse (1-5, default 3).
        """
        return (
            f"Perform an impact analysis for CI **{ci_identifier}**. "
            "Follow these steps in order:\n\n"
            f"1. Call `search_cis` with name_filter='{ci_identifier}' to find the CI. "
            "If that returns no results, try `get_ci_details` with sys_id='{ci_identifier}' instead.\n"
            "2. Once you have the CI's sys_id and table, call `get_ci_details` to get its full record.\n"
            f"3. Call `get_dependency_tree` with the sys_id, direction='upstream', depth={depth} "
            "to find everything that depends on this CI.\n"
            "4. Call `get_ci_relationships` with the sys_id, direction='both' to see all direct relationships.\n\n"
            "After gathering all results, produce an impact assessment that includes:\n"
            "- The CI's identity (name, class, status)\n"
            "- Direct upstream and downstream dependencies\n"
            "- All affected services and applications\n"
            "- A risk summary: how many CIs would be impacted and their criticality\n"
            "- Recommendations for reducing single-point-of-failure risk"
        )

    @mcp.prompt(
        name="troubleshoot_ci",
        title="Troubleshoot CI",
        description=(
            "Diagnose issues with a specific CI. Checks relationships, configurables, "
            "identification rules, staleness, and orphan status."
        ),
    )
    async def troubleshoot_ci(ci_identifier: str) -> str:
        """Troubleshoot CI workflow.

        Args:
            ci_identifier: CI name or sys_id to troubleshoot.
        """
        return (
            f"Troubleshoot CI **{ci_identifier}**. "
            "Follow these steps in order:\n\n"
            f"1. Call `search_cis` with name_filter='{ci_identifier}' to find the CI. "
            "If no results, try `get_ci_details` with sys_id='{ci_identifier}'.\n"
            "2. Once you have the CI, call `get_ci_details` to get its full record. "
            "Note the sys_class_name (its table).\n"
            "3. Call `get_ci_relationships` with the sys_id, direction='both' to check "
            "if it has relationships (orphan check).\n"
            "4. Call `analyze_configurables` with the CI's table to see what business rules, "
            "flows, client scripts, and ACLs affect this CI type.\n"
            "5. Call `get_identification_rules` filtered to the CI's table to understand "
            "how this CI is identified and matched during imports.\n\n"
            "After gathering all results, produce a diagnostic report that includes:\n"
            "- CI identity and current status\n"
            "- Relationship health (orphan? missing key dependencies?)\n"
            "- Staleness check (when was it last updated? by what source?)\n"
            "- Configurables summary (how many BRs, flows, scripts touch this table?)\n"
            "- IRE rules (how is this CI identified? any risk of duplicates?)\n"
            "- Recommendations for resolving any issues found"
        )

    @mcp.prompt(
        name="audit_configurables",
        title="Audit Configurables",
        description=(
            "Audit all configurables touching a CMDB table. Lists business rules, flows, "
            "client scripts, and ACLs with potential conflict analysis."
        ),
    )
    async def audit_configurables(table: str) -> str:
        """Audit Configurables workflow.

        Args:
            table: The CMDB table to audit (e.g. cmdb_ci_server).
        """
        return (
            f"Audit all configurables for the **{table}** table. "
            "Follow these steps in order:\n\n"
            f"1. Call `get_business_rules` for table '{table}' to list all business rules.\n"
            f"2. Call `get_flows` for table '{table}' to list all Flow Designer flows.\n"
            f"3. Call `get_client_scripts` for table '{table}' to list all client scripts.\n"
            f"4. Call `get_acls` for table '{table}' to list all ACL rules.\n\n"
            "After gathering all results, produce a configurables audit report that includes:\n"
            "- Inventory: count of each configurable type (BRs, flows, client scripts, ACLs)\n"
            "- For each business rule: name, when it runs (before/after/async), active status\n"
            "- For each flow: name, trigger, active status\n"
            "- For each client script: name, type (onChange/onLoad/onSubmit), active status\n"
            "- Potential conflicts: multiple BRs on the same event, overlapping ACLs, "
            "scripts that might interfere with each other\n"
            "- Recommendations for simplification or risk reduction"
        )
