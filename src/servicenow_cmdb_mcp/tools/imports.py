"""Import inspection tools — data sources, import set runs, transform errors."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError

logger = logging.getLogger(__name__)

_MAX_LIMIT = 1000


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _clamp_offset(offset: int) -> int:
    return max(0, offset)


def _validate_table_name(table: str) -> str | None:
    """Validate table name contains only safe characters."""
    if not table or not table.strip():
        return "table must not be empty."
    if not all(c.isalnum() or c == "_" for c in table):
        return f"Invalid table name: '{table}'. Must contain only letters, digits, and underscores."
    return None


def _json(result: Any) -> str:
    return json.dumps(result, indent=2, default=str)


def register_import_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all import inspection tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def list_data_sources(
        target_table: str = "",
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List configured import data sources.

        Data sources define where external data comes from (JDBC, LDAP, file,
        REST, etc.) and which import set table it lands in. Use this to
        understand what feeds data into the CMDB.

        Args:
            target_table: Filter by target table name (e.g. cmdb_ci_server). Optional.
            active_only: If True, return only active data sources. Defaults to True.
            limit: Maximum data sources to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "data_sources" list containing
            name, import_set_table, type, target_table, active status, and
            last run timestamp.
        """
        logger.info("list_data_sources: target_table=%s", target_table)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if target_table:
            if err := _validate_table_name(target_table):
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": err, "suggestion": "Provide a valid table name.",
                    "retry": False,
                })

        try:
            query_parts: list[str] = []
            if target_table:
                # CONTAINS is intentional — staging tables have prefixes (e.g. u_cmdb_ci_server_import)
                query_parts.append(f"import_set_table_nameCONTAINS{target_table}")
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="sys_data_source",
                query=query,
                fields=[
                    "sys_id", "name", "import_set_table_name", "type",
                    "active", "sys_updated_on",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYname",
            )

            sources = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "import_set_table": r.get("import_set_table_name", ""),
                    "type": r.get("type", ""),
                    "active": r.get("active", ""),
                    "sys_updated_on": r.get("sys_updated_on", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(sources),
                "target_table_filter": target_table or "(all)",
                "data_sources": sources,
            })
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_import_set_runs(
        table_name: str = "",
        state: str = "",
        days: int = 7,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get recent import set run records.

        Each import set run represents a batch of data loaded into a staging
        table. Shows whether the import completed, how many rows were
        processed, and any errors encountered.

        Args:
            table_name: Filter by import set table name (STARTSWITH match). Optional.
            state: Filter by run state (e.g. "Loaded", "Error", "Processed"). Optional.
            days: How far back to search in days (1-365, default 7).
            limit: Maximum runs to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", "days_back", and "import_set_runs" list
            containing sys_id, table_name, state, row counts, and timestamps.
        """
        logger.info("get_import_set_runs: table_name=%s state=%s days=%d", table_name, state, days)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        if table_name:
            if err := _validate_table_name(table_name):
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": err, "suggestion": "Provide a valid table name.",
                    "retry": False,
                })

        # Validate state doesn't contain query operators
        if state and "^" in state:
            return _json({
                "error": True, "category": "ValidationError",
                "message": "state must not contain query operators.",
                "suggestion": "Remove '^' characters from filter values.",
                "retry": False,
            })

        try:
            query_parts = [f"sys_created_on>=javascript:gs.daysAgo({days})"]
            if table_name:
                query_parts.append(f"table_nameSTARTSWITH{table_name}")
            if state:
                query_parts.append(f"state={state}")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="sys_import_set_run",
                query=query,
                fields=[
                    "sys_id", "table_name", "state", "count",
                    "insert_count", "update_count", "error_count",
                    "data_source", "sys_created_on", "completed",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYDESCsys_created_on",
            )

            runs = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "table_name": r.get("table_name", ""),
                    "state": r.get("state", ""),
                    "total_rows": r.get("count", ""),
                    "insert_count": r.get("insert_count", ""),
                    "update_count": r.get("update_count", ""),
                    "error_count": r.get("error_count", ""),
                    "data_source": r.get("data_source", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                    "completed": r.get("completed", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(runs),
                "days_back": days,
                "import_set_runs": runs,
            })
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_transform_errors(
        target_table: str = "",
        days: int = 7,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get recent transform map errors from import set processing.

        Transform maps control how staging table rows are mapped to target
        CMDB tables. This tool returns rows that failed during transformation,
        showing the error message and which source/target records were involved.

        Args:
            target_table: Filter by target table name (e.g. cmdb_ci_server). Optional.
            days: How far back to search in days (1-365, default 7).
            limit: Maximum errors to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", "days_back", and "transform_errors" list
            containing sys_id, transform_map, target_table, error message,
            source and target records, and timestamp.
        """
        logger.info("get_transform_errors: target_table=%s days=%d", target_table, days)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        if target_table:
            if err := _validate_table_name(target_table):
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": err, "suggestion": "Provide a valid table name.",
                    "retry": False,
                })

        try:
            query_parts = [
                f"sys_created_on>=javascript:gs.daysAgo({days})",
                "status=error",
            ]
            if target_table:
                query_parts.append(f"sys_target_tableSTARTSWITH{target_table}")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="sys_import_set_row",
                query=query,
                fields=[
                    "sys_id", "sys_import_set", "sys_transform_map",
                    "sys_target_table", "sys_target_sys_id",
                    "error_message", "status", "sys_created_on",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYDESCsys_created_on",
            )

            errors = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "import_set": r.get("sys_import_set", ""),
                    "transform_map": r.get("sys_transform_map", ""),
                    "target_table": r.get("sys_target_table", ""),
                    "target_sys_id": r.get("sys_target_sys_id", ""),
                    "error_message": r.get("error_message", ""),
                    "status": r.get("status", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(errors),
                "days_back": days,
                "transform_errors": errors,
            })
        except ServiceNowError as e:
            return e.to_json()
