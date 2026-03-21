"""Configurable inspection tools — business rules, flows, client scripts, ACLs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.redaction import redact_credentials
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _has_more,
    _json,
    _require_client,
    _safe_total,
    _validate_table_name,
)

logger = logging.getLogger(__name__)


def _redact_script_fields(record: dict[str, Any], script_fields: list[str]) -> dict[str, Any]:
    """Redact credential patterns from script body fields in a record."""
    redacted = dict(record)
    for field in script_fields:
        if field in redacted and isinstance(redacted[field], str) and redacted[field]:
            redacted[field] = redact_credentials(redacted[field])
    return redacted


def register_configurable_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all configurable inspection tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_business_rules(
        table: str,
        active_only: bool = True,
        include_scripts: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get business rules configured on a CMDB table.

        Returns server-side business rules that fire on insert, update, delete,
        or query for the specified table. Script bodies are redacted for credentials.

        Args:
            table: The CMDB table to inspect (e.g. cmdb_ci_server, cmdb_ci).
            active_only: If True, return only active rules. Defaults to True.
            include_scripts: If True, include full (redacted) script bodies. Defaults to
                           False for token efficiency — set True when you need to review logic.
            limit: Maximum rules to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "table", "count", "total_count", "has_more", "next_offset",
            and "business_rules" list containing name, when, operations, order, condition,
            and optionally the redacted script body.
        """
        logger.info("get_business_rules: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid table name.",
                "retry": False,
            })

        try:
            query_parts = [f"collection={table}"]
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_script",
                    query=query,
                    fields=[
                        "sys_id", "name", "collection", "active",
                        "when", "action_insert", "action_update", "action_delete", "action_query",
                        "order", "condition", "script",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYorder",
                ),
                _safe_total(client, "sys_script", query),
            )

            rules = []
            for r in records:
                operations = []
                if r.get("action_insert") == "true":
                    operations.append("insert")
                if r.get("action_update") == "true":
                    operations.append("update")
                if r.get("action_delete") == "true":
                    operations.append("delete")
                if r.get("action_query") == "true":
                    operations.append("query")

                rule: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "active": r.get("active", ""),
                    "when": r.get("when", ""),
                    "operations": operations,
                    "order": r.get("order", ""),
                    "condition": r.get("condition", ""),
                }
                if include_scripts:
                    redacted = _redact_script_fields(r, ["script"])
                    rule["script"] = redacted.get("script", "")
                rules.append(rule)

            result: dict[str, Any] = {
                "table": table,
                "count": len(rules),
                "business_rules": rules,
                "suggested_next": f"Use get_client_scripts(table='{table}') for UI-side scripts, get_acls(table='{table}') for access controls, or analyze_configurables(table='{table}') for a full summary.",
            }
            result["total_count"] = total
            result["has_more"] = _has_more(total, offset, len(rules), limit)
            result["next_offset"] = offset + len(rules)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_client_scripts(
        table: str,
        active_only: bool = True,
        include_scripts: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get client scripts configured on a CMDB table.

        Returns UI-side scripts (onChange, onLoad, onSubmit, onCellEdit) for the
        specified table. Script bodies are redacted for credentials.

        Args:
            table: The CMDB table to inspect (e.g. cmdb_ci_server).
            active_only: If True, return only active scripts. Defaults to True.
            include_scripts: If True, include full (redacted) script bodies. Defaults to
                           False for token efficiency — set True when you need to review logic.
            limit: Maximum scripts to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "table", "count", "total_count", "has_more", "next_offset",
            and "client_scripts" list containing name, type, field_name, and optionally
            the redacted script body.
        """
        logger.info("get_client_scripts: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid table name.",
                "retry": False,
            })

        try:
            query_parts = [f"table={table}"]
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_script_client",
                    query=query,
                    fields=[
                        "sys_id", "name", "table", "active", "type",
                        "field", "script",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "sys_script_client", query),
            )

            scripts = []
            for r in records:
                entry: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "active": r.get("active", ""),
                    "type": r.get("type", ""),
                    "field_name": r.get("field", ""),
                }
                if include_scripts:
                    redacted = _redact_script_fields(r, ["script"])
                    entry["script"] = redacted.get("script", "")
                scripts.append(entry)

            result: dict[str, Any] = {
                "table": table,
                "count": len(scripts),
                "client_scripts": scripts,
                "suggested_next": f"Use get_business_rules(table='{table}') for server-side scripts, get_acls(table='{table}') for access controls, or analyze_configurables(table='{table}') for a full summary.",
            }
            result["total_count"] = total
            result["has_more"] = _has_more(total, offset, len(scripts), limit)
            result["next_offset"] = offset + len(scripts)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_flows(
        table: str,
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get Flow Designer flows related to a CMDB table.

        Searches sys_hub_flow for flows whose internal_name references the
        specified table. Note: flow trigger/action details are stored in
        sub-tables, so this provides an overview — use the ServiceNow UI
        for full flow logic inspection.

        Limitation: Matches flows by internal_name CONTAINS table — flows
        referencing the table indirectly (e.g. via subflow or action) may
        not appear. If no results are returned, verify in the ServiceNow
        Flow Designer UI directly.

        Args:
            table: The CMDB table to find flows for (e.g. cmdb_ci_server).
            active_only: If True, return only active flows. Defaults to True.
            limit: Maximum flows to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "table", "count", and "flows" list containing
            name, description, active status, and run_as.
        """
        logger.info("get_flows: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid table name.",
                "retry": False,
            })

        try:
            query_parts = [f"internal_nameCONTAINS{table}"]
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_hub_flow",
                    query=query,
                    fields=[
                        "sys_id", "name", "internal_name", "description",
                        "active", "run_as",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "sys_hub_flow", query),
            )

            flows = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "internal_name": r.get("internal_name", ""),
                    "description": r.get("description", ""),
                    "active": r.get("active", ""),
                    "run_as": r.get("run_as", ""),
                }
                for r in records
            ]

            result: dict[str, Any] = {
                "table": table,
                "count": len(flows),
                "flows": flows,
                "suggested_next": f"Use get_business_rules(table='{table}') for server-side logic, get_acls(table='{table}') for access controls, or analyze_configurables(table='{table}') for a full overview.",
            }
            result["total_count"] = total
            result["has_more"] = _has_more(total, offset, len(flows), limit)
            result["next_offset"] = offset + len(flows)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_acls(
        table: str,
        active_only: bool = True,
        include_scripts: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get ACL rules governing access to a CMDB table.

        Returns access control list entries from sys_security_acl for the
        specified table. Shows which roles can read, write, create, or delete
        records. Script conditions are redacted for credentials.

        Args:
            table: The CMDB table to inspect (e.g. cmdb_ci_server).
            active_only: If True, return only active ACLs. Defaults to True.
            include_scripts: If True, include full (redacted) script bodies. Defaults to
                           False for token efficiency — set True when you need to review logic.
            limit: Maximum ACLs to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "table", "count", "total_count", "has_more", "next_offset",
            and "acls" list containing name, operation, type, admin_overrides, condition,
            and optionally the redacted script body.
        """
        logger.info("get_acls: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid table name.",
                "retry": False,
            })

        try:
            query_parts = [f"nameSTARTSWITH{table}"]
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_security_acl",
                    query=query,
                    fields=[
                        "sys_id", "name", "operation", "type", "active",
                        "admin_overrides", "condition", "script",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "sys_security_acl", query),
            )

            acls = []
            for r in records:
                entry: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "operation": r.get("operation", ""),
                    "type": r.get("type", ""),
                    "active": r.get("active", ""),
                    "admin_overrides": r.get("admin_overrides", ""),
                    "condition": r.get("condition", ""),
                }
                if include_scripts:
                    redacted = _redact_script_fields(r, ["script"])
                    entry["script"] = redacted.get("script", "")
                acls.append(entry)

            result: dict[str, Any] = {
                "table": table,
                "count": len(acls),
                "acls": acls,
                "suggested_next": f"Use get_business_rules(table='{table}') for server-side scripts, get_client_scripts(table='{table}') for UI scripts, or analyze_configurables(table='{table}') for a full summary.",
            }
            result["total_count"] = total
            result["has_more"] = _has_more(total, offset, len(acls), limit)
            result["next_offset"] = offset + len(acls)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def analyze_configurables(
        table: str,
    ) -> str:
        """Produce a summary of all configurables for a CMDB table.

        Counts business rules, client scripts, flows, and ACLs for the given
        table in a single overview. Uses the Aggregate API for efficient counting
        where possible, falling back to limited record fetches.

        Use this for a quick audit of what automation and access controls exist
        on a table before making changes.

        Args:
            table: The CMDB table to analyze (e.g. cmdb_ci_server).

        Returns:
            JSON object with "table" and counts for each configurable type:
            "business_rules", "client_scripts", "flows", "acls", each with
            "active_count" and "total_count".
        """
        logger.info("analyze_configurables: table=%s", table)
        if err := _require_client(client):
            return err

        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid table name.",
                "retry": False,
            })

        def _count(agg: dict[str, Any]) -> int:
            result = agg.get("result", agg)
            if isinstance(result, dict):
                try:
                    return int(result.get("stats", {}).get("count", 0))
                except (ValueError, TypeError):
                    return 0
            return 0

        async def _safe_aggregate(agg_table: str, query: str) -> dict[str, Any]:
            """Run an aggregate call, returning an error marker on failure."""
            try:
                return await client.get_aggregate(table=agg_table, query=query)
            except ServiceNowError:
                return {"_error": True}

        results = await asyncio.gather(
            _safe_aggregate("sys_script", f"collection={table}"),
            _safe_aggregate("sys_script", f"collection={table}^active=true"),
            _safe_aggregate("sys_script_client", f"table={table}"),
            _safe_aggregate("sys_script_client", f"table={table}^active=true"),
            _safe_aggregate("sys_hub_flow", f"internal_nameCONTAINS{table}"),
            _safe_aggregate("sys_hub_flow", f"internal_nameCONTAINS{table}^active=true"),
            _safe_aggregate("sys_security_acl", f"nameSTARTSWITH{table}"),
            _safe_aggregate("sys_security_acl", f"nameSTARTSWITH{table}^active=true"),
        )

        def _category(total: dict, active: dict) -> dict[str, Any]:
            if total.get("_error") or active.get("_error"):
                return {"total_count": None, "active_count": None, "error": "Access denied or unavailable"}
            return {"total_count": _count(total), "active_count": _count(active)}

        return _json({
            "table": table,
            "business_rules": _category(results[0], results[1]),
            "client_scripts": _category(results[2], results[3]),
            "flows": _category(results[4], results[5]),
            "acls": _category(results[6], results[7]),
            "suggested_next": f"Use get_business_rules(table='{table}'), get_client_scripts(table='{table}'), get_acls(table='{table}'), or get_flows(table='{table}') for details.",
        })
