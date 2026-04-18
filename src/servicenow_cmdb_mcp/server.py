"""FastMCP server setup and entry point."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import _json
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
    client, and registers all tool and resource modules.  If credentials are
    missing, the server still starts so that tool metadata can be introspected;
    tools will return errors when called without a configured client.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception:
        logger.warning(
            "ServiceNow credentials not configured — server will start "
            "but tools will fail until environment variables are set."
        )
        settings = None

    client = ServiceNowClient(settings) if settings else None  # type: ignore[arg-type]
    cache = MetadataCache(ttl=settings.cache_ttl if settings else 3600)

    mcp = FastMCP(
        "ServiceNow CMDB",
        instructions="MCP server connecting Claude to ServiceNow CMDB via natural language",
    )

    # ── Health check tool ────────────────────────────────────────────────
    # Registered first so agents can verify connectivity before any workflow.

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def check_connection() -> str:
        """Check connectivity and authentication to the ServiceNow instance.

        Call this tool at the start of any workflow to verify that:
        1. ServiceNow credentials are configured
        2. The instance is reachable
        3. The authenticated user has valid access

        Returns the instance URL, authenticated username, and directly-assigned
        roles (inherited roles are not included). No parameters required.

        Returns:
            JSON object with "connected" (bool), "instance_url", "authenticated_as",
            and "roles" (list of directly-assigned role names), or an error if
            connection fails.
        """
        logger.info("check_connection")

        if client is None:
            return _json({
                "connected": False,
                "error": True,
                "category": "AuthError",
                "message": "ServiceNow credentials are not configured.",
                "suggestion": "Set environment variables: SN_INSTANCE_URL, SN_CLIENT_ID, SN_CLIENT_SECRET, SN_USERNAME, SN_PASSWORD.",
                "retry": False,
            })

        try:
            # settings is guaranteed non-None when client is non-None (see create_app)
            if settings is None:
                return _json({
                    "error": True, "category": "AuthError",
                    "message": "Settings not available.", "suggestion": "Restart with credentials configured.", "retry": False,
                })
            username = settings.username
            response = await client.get(
                "/api/now/table/sys_user",
                params={
                    "sysparm_query": f"user_name={username}",
                    "sysparm_limit": "1",
                    "sysparm_fields": "user_name,sys_id",
                },
            )
            records = response.get("result", [])
            if records:
                user = records[0]
                user_sys_id = user.get("sys_id", "")

                # Query sys_user_has_role for directly assigned roles
                roles: list[str] = []
                if user_sys_id:
                    role_response = await client.get(
                        "/api/now/table/sys_user_has_role",
                        params={
                            "sysparm_query": f"user={user_sys_id}^inherited=false",
                            "sysparm_fields": "role",
                            "sysparm_display_value": "true",
                            "sysparm_limit": "100",
                        },
                    )
                    role_records = role_response.get("result", [])
                    for r in role_records:
                        role_val = r.get("role", "")
                        if isinstance(role_val, dict):
                            role_val = role_val.get("display_value", "")
                        if role_val:
                            roles.append(role_val)

                return _json({
                    "connected": True,
                    "instance_url": client.base_url,
                    "authenticated_as": user.get("user_name", username),
                    "user_sys_id": user_sys_id,
                    "roles": roles,
                    "suggested_next": "Use search_cis to query CIs, list_ci_classes to explore the schema, or suggest_table to find the right table.",
                })
            else:
                return _json({
                    "connected": True,
                    "instance_url": client.base_url,
                    "authenticated_as": username,
                    "roles": [],
                    "warning": "Could not retrieve user record — the service account user may not have read access to sys_user.",
                    "suggested_next": "Connection is working. Use search_cis to query CIs.",
                })
        except ServiceNowError as e:
            return e.to_json()

    # ── Diagnostic tool ──────────────────────────────────────────────────
    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def _diag_probe_table(
        table: str,
        query: str = "",
        limit: int = 3,
    ) -> str:
        """TEMPORARY diagnostic tool. Probe any ServiceNow table to check access.

        Args:
            table: Table to probe (e.g. sys_hub_step_instance).
            query: Optional encoded query filter.
            limit: Max records to return (default 3).
        """
        if client is None:
            return _json({"error": True, "message": "No client"})
        try:
            records = await client.get_records(
                table=table,
                query=query,
                limit=limit,
            )
            return _json({
                "table": table,
                "count": len(records),
                "fields": list(records[0].keys()) if records else [],
                "sample": records[:2] if records else [],
            })
        except ServiceNowError as e:
            return e.to_json()

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

    if sys.platform == "win32":
        # On Windows, Python's TextIOWrapper with newline=None (the default) translates
        # \n → \r\n on stdout.  The MCP NDJSON transport writes one JSON object per line;
        # the extra \r turns each line into "json\r" which the Node.js MCP client (Claude
        # Code) can't parse, causing "Failed to reconnect".
        # Fix: override run_stdio_async to inject wrappers with newline="\n".
        import io as _io
        import anyio as _anyio
        from mcp.server.stdio import stdio_server as _stdio_server

        _mcp_server = app._mcp_server  # type: ignore[attr-defined]

        async def _run_stdio_windows() -> None:
            _stdin = _anyio.wrap_file(
                _io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")
            )
            _stdout = _anyio.wrap_file(
                _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
            )
            async with _stdio_server(stdin=_stdin, stdout=_stdout) as (_r, _w):
                await _mcp_server.run(_r, _w, _mcp_server.create_initialization_options())

        app.run_stdio_async = _run_stdio_windows  # type: ignore[method-assign]

    app.run(transport="stdio")


if __name__ == "__main__":
    main()
