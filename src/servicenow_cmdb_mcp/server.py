"""FastMCP server setup and entry point."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.prompts.workflows import register_prompts
from servicenow_cmdb_mcp.resources.schema import register_schema_resources
from servicenow_cmdb_mcp.tools.configurables import register_configurable_tools
from servicenow_cmdb_mcp.tools.discovery import register_discovery_tools
from servicenow_cmdb_mcp.tools.health import register_health_tools
from servicenow_cmdb_mcp.tools.imports import register_import_tools
from servicenow_cmdb_mcp.tools.ire import register_ire_tools
from servicenow_cmdb_mcp.tools.mutations import register_mutation_tools
from servicenow_cmdb_mcp.tools.queries import register_query_tools
from servicenow_cmdb_mcp.tools.relationships import register_relationship_tools

logger = logging.getLogger(__name__)


def create_app() -> FastMCP:
    """Create and configure the MCP server with all tools and resources.

    Initializes settings from environment variables, creates the ServiceNow
    client, and registers all tool and resource modules.
    """
    settings = Settings()  # type: ignore[call-arg]
    client = ServiceNowClient(settings)
    cache = MetadataCache(ttl=settings.cache_ttl)

    mcp = FastMCP(
        "ServiceNow CMDB",
        instructions="MCP server connecting Claude to ServiceNow CMDB via natural language",
    )

    # ── Tool registration ────────────────────────────────────────────────
    # Each domain module exports a register_*_tools(mcp, client) function.

    register_query_tools(mcp, client, cache)
    register_relationship_tools(mcp, client, cache)
    register_health_tools(mcp, client)
    register_mutation_tools(mcp, client)
    register_configurable_tools(mcp, client)
    register_discovery_tools(mcp, client)
    register_ire_tools(mcp, client)
    register_import_tools(mcp, client)

    # ── Resource registration ────────────────────────────────────────────
    register_schema_resources(mcp, client, cache)

    # ── Prompt registration ──────────────────────────────────────────────
    register_prompts(mcp)

    return mcp


def main() -> None:
    """Entry point for the MCP server (STDIO transport)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting ServiceNow CMDB MCP server")
    app = create_app()
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
