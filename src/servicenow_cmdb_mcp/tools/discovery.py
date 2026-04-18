"""Discovery inspection tools — schedules and error logs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

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
    _validation_error,
)

logger = logging.getLogger(__name__)

# Valid filter values for discovery status and error severity
VALID_DISCOVERY_STATES = {"Starting", "Active", "Completed", "Cancelled", "Error"}
VALID_SEVERITIES = {"Error", "Warning", "Info"}


def register_discovery_tools(mcp: FastMCP, client: ServiceNowClient | None) -> None:
    """Register all discovery inspection tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
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
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        try:
            query_parts: list[str] = []
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="discovery_schedule",
                    query=query,
                    fields=[
                        "sys_id", "name", "active", "discover",
                        "run_as", "sys_updated_on",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "discovery_schedule", query),
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

            result: dict[str, Any] = {
                "count": len(schedules),
                "schedules": schedules,
                "suggested_next": "Use get_discovery_status(schedule_name) to see recent scan results, or get_discovery_errors() for error logs.",
            }
            result.update(_pagination_metadata(total, offset, len(schedules), limit))
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
    async def get_discovery_status(
        schedule_name: str = "",
        state: Literal["", "Starting", "Active", "Completed", "Cancelled", "Error"] = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get recent Discovery status records showing scan results.

        Queries the discovery_status table for recent discovery runs. Each
        record represents a single discovery scan attempt against a target,
        showing whether it succeeded, failed, or is in progress.

        Args:
            schedule_name: Filter by schedule name (STARTSWITH match). Optional.
            state: Filter by discovery state. Valid values: "Starting", "Active",
                  "Completed", "Cancelled", "Error". Optional.
            limit: Maximum records to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "statuses" list containing
            sys_id, state, source, target IP, CI created/updated info,
            and timestamps.
        """
        logger.info("get_discovery_status: schedule_name=%s state=%s", schedule_name, state)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        # Validate filter values don't contain query operators
        for val in (schedule_name, state):
            if val and "^" in val:
                return _validation_error(
                    "Filter values must not contain query operators.",
                    "Remove '^' characters from filter values.",
                    "Use list_discovery_schedules() to browse valid schedule names.",
                )

        if state and state not in VALID_DISCOVERY_STATES:
            return _validation_error(
                f"Invalid state '{state}'. "
                f"Valid values: {', '.join(sorted(VALID_DISCOVERY_STATES))}.",
                "Use one of the valid discovery state values.",
                "Valid states: Starting, Active, Completed, Cancelled, Error.",
            )

        try:
            query_parts: list[str] = []
            if schedule_name:
                query_parts.append(f"dsc_scheduleSTARTSWITH{schedule_name}")
            if state:
                query_parts.append(f"state={state}")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
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
                ),
                _safe_total(client, "discovery_status", query),
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

            result: dict[str, Any] = {
                "count": len(statuses),
                "statuses": statuses,
                "suggested_next": "Use get_discovery_errors() to see error details for failed scans, or get_ci_details(sys_id) on a discovered CI.",
            }
            result.update(_pagination_metadata(total, offset, len(statuses), limit))
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
    async def get_discovery_errors(
        severity: Literal["", "Error", "Warning", "Info"] = "Error",
        days: int = 7,
        limit: int = 25,
        offset: int = 0,
        max_message_length: int = 500,
    ) -> str:
        """Get recent Discovery error and warning log entries.

        Queries the discovery_log table for error/warning entries from
        recent discovery runs. Use this to diagnose why CIs are not being
        discovered or updated correctly.

        Args:
            severity: Filter by severity level. Defaults to "Error".
                      Set to "" (empty string) to return all severity levels.
            days: How far back to search in days (1-365, default 7).
            limit: Maximum log entries to return (1-1000, default 25).
            offset: Pagination offset.
            max_message_length: Truncate message fields longer than this (default 500).
                               Set to 0 to return full messages. Truncated messages include
                               a message_length field with the original character count.

        Returns:
            JSON object with "count", "days_back", and "errors" list containing
            sys_id, level, message, source, CI reference, and timestamp.
        """
        logger.info("get_discovery_errors: severity=%s days=%d", severity, days)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 365))

        if severity and severity not in VALID_SEVERITIES:
            return _validation_error(
                f"Invalid severity '{severity}'. "
                f"Valid values: {', '.join(sorted(VALID_SEVERITIES))}.",
                "Use 'Error', 'Warning', or 'Info'.",
                "Valid severity levels: Error, Warning, Info.",
            )

        try:
            query_parts = [f"sys_created_on>=javascript:gs.daysAgo({days})"]
            if severity:
                query_parts.append(f"level={severity}")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="discovery_log",
                    query=query,
                    fields=[
                        "sys_id", "level", "message", "source",
                        "cmdb_ci", "status", "sys_created_on",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYDESCsys_created_on",
                ),
                _safe_total(client, "discovery_log", query),
            )

            max_len = max(0, max_message_length)
            errors: list[dict[str, Any]] = []
            for r in records:
                msg = r.get("message", "")
                entry: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "level": r.get("level", ""),
                    "message": msg[:max_len] + "…" if max_len and len(msg) > max_len else msg,
                    "source": r.get("source", ""),
                    "cmdb_ci": r.get("cmdb_ci", ""),
                    "status": r.get("status", ""),
                    "sys_created_on": r.get("sys_created_on", ""),
                }
                if max_len and len(msg) > max_len:
                    entry["message_length"] = len(msg)
                errors.append(entry)

            result: dict[str, Any] = {
                "count": len(errors),
                "days_back": days,
                "errors": errors,
                "suggested_next": "Use get_ci_details(sys_id) to inspect a CI referenced in an error, or list_discovery_schedules() to review schedule configuration.",
            }
            result.update(_pagination_metadata(total, offset, len(errors), limit))
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()
