"""Discovery inspection tools — schedules and error logs."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _json,
)

logger = logging.getLogger(__name__)


def register_discovery_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all discovery inspection tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def list_discovery_schedules(
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Discovery schedules configured in the instance.

        Returns discovery schedules from the discovery_schedule table, showing
        schedule name, status, frequency, and which IP ranges or CI groups
        are targeted. Use this to understand what automated discovery is
        running and when.

        Args:
            active_only: If True, return only active schedules. Defaults to True.
            limit: Maximum schedules to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "schedules" list containing
            name, active status, discover, frequency, and run_as.
        """
        logger.info("list_discovery_schedules: active_only=%s", active_only)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        try:
            query_parts: list[str] = []
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="discovery_schedule",
                query=query,
                fields=[
                    "sys_id", "name", "active", "discover",
                    "run_as", "sys_updated_on",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYname",
            )

            schedules = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "active": r.get("active", ""),
                    "discover": r.get("discover", ""),
                    "run_as": r.get("run_as", ""),
                    "sys_updated_on": r.get("sys_updated_on", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(schedules),
                "schedules": schedules,
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
    async def get_discovery_status(
        schedule_name: str = "",
        state: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get recent Discovery status records showing scan results.

        Queries the discovery_status table for recent discovery runs. Each
        record represents a single discovery scan attempt against a target,
        showing whether it succeeded, failed, or is in progress.

        Args:
            schedule_name: Filter by schedule name (STARTSWITH match). Optional.
            state: Filter by state (e.g. "Completed", "Error", "Active"). Optional.
            limit: Maximum records to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "statuses" list containing
            sys_id, state, source, target IP, CI created/updated info,
            and timestamps.
        """
        logger.info("get_discovery_status: schedule_name=%s state=%s", schedule_name, state)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        # Validate filter values don't contain query operators
        for val in (schedule_name, state):
            if val and "^" in val:
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": "Filter values must not contain query operators.",
                    "suggestion": "Remove '^' characters from filter values.",
                    "retry": False,
                })

        try:
            query_parts: list[str] = []
            if schedule_name:
                query_parts.append(f"dsc_scheduleSTARTSWITH{schedule_name}")
            if state:
                query_parts.append(f"state={state}")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="discovery_status",
                query=query,
                fields=[
                    "sys_id", "state", "source", "ip_address",
                    "dsc_schedule", "cmdb_ci", "sys_created_on",
                    "started", "completed",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYDESCsys_created_on",
            )

            statuses = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "state": r.get("state", ""),
                    "source": r.get("source", ""),
                    "ip_address": r.get("ip_address", ""),
                    "schedule": r.get("dsc_schedule", ""),
                    "cmdb_ci": r.get("cmdb_ci", ""),
                    "started": r.get("started", ""),
                    "completed": r.get("completed", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(statuses),
                "statuses": statuses,
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
    async def get_discovery_errors(
        severity: str = "",
        days: int = 7,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get recent Discovery error and warning log entries.

        Queries the discovery_log table for error/warning entries from
        recent discovery runs. Use this to diagnose why CIs are not being
        discovered or updated correctly.

        Args:
            severity: Filter by severity level (e.g. "Error", "Warning"). Optional —
                      defaults to showing errors only.
            days: How far back to search in days (1-365, default 7).
            limit: Maximum log entries to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", "days_back", and "errors" list containing
            sys_id, level, message, source, CI reference, and timestamp.
        """
        logger.info("get_discovery_errors: severity=%s days=%d", severity, days)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        # Validate severity doesn't contain query operators
        if severity and "^" in severity:
            return _json({
                "error": True, "category": "ValidationError",
                "message": "Severity must not contain query operators.",
                "suggestion": "Use a simple value like 'Error' or 'Warning'.",
                "retry": False,
            })

        try:
            query_parts = [f"sys_created_on>=javascript:gs.daysAgo({days})"]
            if severity:
                query_parts.append(f"level={severity}")
            else:
                query_parts.append("level=Error")
            query = "^".join(query_parts)

            records = await client.get_records(
                table="discovery_log",
                query=query,
                fields=[
                    "sys_id", "level", "message", "source",
                    "cmdb_ci", "status", "sys_created_on",
                ],
                limit=limit,
                offset=offset,
                order_by="ORDERBYDESCsys_created_on",
            )

            errors = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "level": r.get("level", ""),
                    "message": r.get("message", ""),
                    "source": r.get("source", ""),
                    "cmdb_ci": r.get("cmdb_ci", ""),
                    "status": r.get("status", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                }
                for r in records
            ]

            return _json({
                "count": len(errors),
                "days_back": days,
                "errors": errors,
            })
        except ServiceNowError as e:
            return e.to_json()
