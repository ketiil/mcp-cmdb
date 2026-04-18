"""Import inspection tools — data sources, import set runs, transform errors."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _json,
    _pagination_metadata,
    _require_client,
    _safe_total,
    _validate_table_name,
    _validation_error,
)

logger = logging.getLogger(__name__)


def register_import_tools(mcp: FastMCP, client: ServiceNowClient | None) -> None:
    """Register all import inspection tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
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
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if target_table:
            if err := _validate_table_name(target_table):
                return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

        try:
            query_parts: list[str] = []
            if target_table:
                # CONTAINS is intentional — staging tables have prefixes (e.g. u_cmdb_ci_server_import)
                query_parts.append(f"import_set_table_nameCONTAINS{target_table}")
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_data_source",
                    query=query,
                    fields=[
                        "sys_id", "name", "import_set_table_name", "type",
                        "active", "sys_updated_on",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "sys_data_source", query),
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

            result: dict[str, Any] = {
                "count": len(sources),
                "target_table_filter": target_table or "(all)",
                "data_sources": sources,
                "suggested_next": "Use get_import_set_runs(table_name) to see recent runs for a data source, or get_transform_errors(target_table) for mapping failures.",
            }
            result.update(_pagination_metadata(total, offset, len(sources), limit))
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
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
            state: Filter by run state. Common values include "Loaded", "Processed",
                  "Error", "Complete", "Complete with errors", "Cancelled". Values
                  may vary by instance configuration. Optional.
            days: How far back to search in days (1-365, default 7).
            limit: Maximum runs to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", "days_back", and "import_set_runs" list
            containing sys_id, table_name, state, row counts, and timestamps.
        """
        logger.info("get_import_set_runs: table_name=%s state=%s days=%d", table_name, state, days)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        if table_name:
            if err := _validate_table_name(table_name):
                return _validation_error(err, "Provide a valid table name.", "Use list_data_sources() to browse available import set table names.")

        # Validate state doesn't contain query operators
        if state and "^" in state:
            return _validation_error(
                "state must not contain query operators.",
                "Remove '^' characters from filter values.",
                "Common states: Loaded, Processed, Complete, Error, Cancelled.",
            )

        try:
            query_parts = [f"sys_created_on>=javascript:gs.daysAgo({days})"]
            if table_name:
                query_parts.append(f"table_nameSTARTSWITH{table_name}")
            if state:
                query_parts.append(f"state={state}")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
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
                ),
                _safe_total(client, "sys_import_set_run", query),
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

            result: dict[str, Any] = {
                "count": len(runs),
                "days_back": days,
                "import_set_runs": runs,
                "suggested_next": "Use get_transform_errors(target_table) to see row-level errors, or list_data_sources() to review source configuration.",
            }
            result.update(_pagination_metadata(total, offset, len(runs), limit))
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def get_transform_errors(
        target_table: str = "",
        days: int = 7,
        limit: int = 25,
        offset: int = 0,
        max_error_length: int = 500,
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
            max_error_length: Truncate error_message fields longer than this (default 500).
                             Set to 0 to return full messages. Truncated messages include
                             an error_message_length field with the original character count.

        Returns:
            JSON object with "count", "days_back", and "transform_errors" list
            containing sys_id, transform_map, target_table, error message,
            source and target records, and timestamp.
        """
        logger.info("get_transform_errors: target_table=%s days=%d", target_table, days)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        if target_table:
            if err := _validate_table_name(target_table):
                return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

        try:
            query_parts = [
                f"sys_created_on>=javascript:gs.daysAgo({days})",
                "status=error",
            ]
            if target_table:
                query_parts.append(f"sys_target_tableSTARTSWITH{target_table}")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
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
                ),
                _safe_total(client, "sys_import_set_row", query),
            )

            max_len = max(0, max_error_length)
            errors: list[dict[str, Any]] = []
            for r in records:
                msg = r.get("error_message", "")
                entry: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "import_set": r.get("sys_import_set", ""),
                    "transform_map": r.get("sys_transform_map", ""),
                    "target_table": r.get("sys_target_table", ""),
                    "target_sys_id": r.get("sys_target_sys_id", ""),
                    "error_message": msg[:max_len] + "…" if max_len and len(msg) > max_len else msg,
                    "status": r.get("status", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                }
                if max_len and len(msg) > max_len:
                    entry["error_message_length"] = len(msg)
                errors.append(entry)

            result: dict[str, Any] = {
                "count": len(errors),
                "days_back": days,
                "transform_errors": errors,
                "suggested_next": "Use get_ci_details(sys_id) to inspect a target CI, or get_import_set_runs() to see the parent import run.",
            }
            result.update(_pagination_metadata(total, offset, len(errors), limit))
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()
