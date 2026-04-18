"""Configurable inspection tools — business rules, flows, client scripts, ACLs."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.redaction import redact_credentials
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _json,
    _not_found_error,
    _pagination_metadata,
    _require_client,
    _safe_total,
    _validate_sys_id,
    _validate_table_name,
    _validation_error,
)

logger = logging.getLogger(__name__)


def _redact_script_fields(record: dict[str, Any], script_fields: list[str]) -> dict[str, Any]:
    """Redact credential patterns from script body fields in a record."""
    redacted = dict(record)
    for field in script_fields:
        if field in redacted and isinstance(redacted[field], str) and redacted[field]:
            redacted[field] = redact_credentials(redacted[field])
    return redacted


def register_configurable_tools(mcp: FastMCP, client: ServiceNowClient | None) -> None:
    """Register all configurable inspection tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
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
            return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

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
            result.update(_pagination_metadata(total, offset, len(rules), limit))
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
            return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

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
            result.update(_pagination_metadata(total, offset, len(scripts), limit))
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
    async def get_flows(
        table: str = "",
        name_filter: str = "",
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get Flow Designer flows related to a CMDB table or matching a name.

        Searches sys_hub_flow by internal_name (CONTAINS table) and/or display
        name (CONTAINS name_filter). At least one of table or name_filter must
        be provided. Note: flow trigger/action details are stored in sub-tables,
        so this provides an overview — use the ServiceNow UI for full flow logic.

        Examples:
            get_flows(table="cmdb_ci_server")
            get_flows(name_filter="decommission")
            get_flows(name_filter="DNB", active_only=False)

        Args:
            table: Filter flows whose internal_name contains this value
                  (e.g. cmdb_ci_server). Optional if name_filter is provided.
            name_filter: Filter flows whose display name contains this value
                        (e.g. "decommission", "DNB"). Optional if table is provided.
            active_only: If True, return only active flows. Defaults to True.
            limit: Maximum flows to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", and "flows" list containing
            name, internal_name, description, active status, and run_as.
        """
        logger.info("get_flows: table=%s name_filter=%s", table, name_filter)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if not table and not name_filter:
            return _validation_error(
                "At least one of 'table' or 'name_filter' must be provided.",
                "Provide a table name to find flows by internal_name, or a name_filter to search by display name.",
            )

        if table:
            if err := _validate_table_name(table):
                return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

        if name_filter and "^" in name_filter:
            return _validation_error(
                "name_filter must not contain encoded query operators ('^').",
                "Provide a plain text search term without special characters.",
            )

        try:
            query_parts: list[str] = []
            if table:
                query_parts.append(f"internal_nameCONTAINS{table}")
            if name_filter:
                query_parts.append(f"nameLIKE{name_filter}")
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
                "count": len(flows),
                "flows": flows,
                "suggested_next": "Use get_business_rules(table) for server-side logic, get_acls(table) for access controls, or analyze_configurables(table) for a full overview.",
            }
            if table:
                result["table"] = table
            if name_filter:
                result["name_filter"] = name_filter
            result.update(_pagination_metadata(total, offset, len(flows), limit))
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
    async def get_flow_details(
        sys_id: str,
    ) -> str:
        """Get detailed logic of a Flow Designer flow by sys_id.

        Fetches the flow definition from sys_hub_flow_base and parses the
        label_cache to extract trigger, steps, referenced tables, and data
        flow. Use get_flows to find the flow sys_id first.

        Prerequisites: Use get_flows(name_filter="...") to find the flow sys_id.

        Examples:
            get_flow_details(sys_id="abc123...")

        Args:
            sys_id: The sys_id of the flow (from get_flows results).

        Returns:
            JSON object with flow metadata (name, description, status, run_as)
            and parsed steps showing the flow's trigger, actions, and data flow.
        """
        logger.info("get_flow_details: sys_id=%s", sys_id)
        if err := _require_client(client):
            return err

        if err := _validate_sys_id(sys_id):
            return _validation_error(
                err,
                "Provide a valid flow sys_id.",
                "Use get_flows(name_filter='...') to find the flow sys_id.",
            )

        try:
            record = await client.get_record(
                table="sys_hub_flow_base",
                sys_id=sys_id,
                fields=[
                    "sys_id", "name", "internal_name", "description", "active",
                    "status", "run_as", "type", "label_cache",
                    "sys_scope", "sys_created_by", "sys_updated_on",
                ],
            )

            if not record:
                return _not_found_error(
                    f"Flow with sys_id '{sys_id}' not found.",
                    "Verify the sys_id or use get_flows to search.",
                    "Use get_flows(name_filter='...') to find available flows.",
                )

            # Parse label_cache into structured steps
            steps: list[dict[str, Any]] = []
            label_cache_raw = record.get("label_cache", "")
            if label_cache_raw:
                try:
                    labels = json.loads(label_cache_raw)
                    if isinstance(labels, list):
                        for entry in labels:
                            step: dict[str, str] = {
                                "label": entry.get("label", ""),
                                "type": entry.get("type", ""),
                            }
                            if entry.get("reference"):
                                step["reference_table"] = entry["reference"]
                            if entry.get("parent_table_name"):
                                step["parent_table"] = entry["parent_table_name"]
                            if entry.get("column_name"):
                                step["column"] = entry["column_name"]
                            steps.append(step)
                except (json.JSONDecodeError, TypeError):
                    steps = [{"error": "Could not parse label_cache"}]

            result: dict[str, Any] = {
                "sys_id": record.get("sys_id", ""),
                "name": record.get("name", ""),
                "internal_name": record.get("internal_name", ""),
                "description": record.get("description", ""),
                "active": record.get("active", ""),
                "status": record.get("status", ""),
                "run_as": record.get("run_as", ""),
                "type": record.get("type", ""),
                "sys_scope": record.get("sys_scope", ""),
                "sys_created_by": record.get("sys_created_by", ""),
                "sys_updated_on": record.get("sys_updated_on", ""),
                "steps": steps,
                "step_count": len(steps),
                "suggested_next": "Use get_flows(name_filter='...') to find other flows, or get_business_rules(table) for server-side logic.",
            }
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
            return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

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
            result.update(_pagination_metadata(total, offset, len(acls), limit))
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
    async def get_script_includes(
        name_filter: str = "",
        active_only: bool = True,
        include_scripts: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get script includes matching a name filter.

        Script includes are reusable server-side JavaScript classes and functions.
        Unlike business rules, they are not tied to a specific table — they are
        global utilities callable from any server-side script.

        Use this to find utility classes referenced by business rules, flows, or
        other scripts (e.g., searching for "CMDB" to find CMDB-related utilities).

        Examples:
            get_script_includes(name_filter="CMDB")
            get_script_includes(name_filter="DNB_CMDB", include_scripts=True)
            get_script_includes(name_filter="Util", active_only=False, limit=50)

        Args:
            name_filter: Filter script includes whose name contains this value
                        (case-insensitive LIKE match). When empty, returns all
                        script includes up to the limit.
            active_only: If True, return only active script includes. Defaults to True.
            include_scripts: If True, include full (redacted) script bodies. Defaults to
                           False for token efficiency — set True when you need to review logic.
            limit: Maximum script includes to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count", "total_count", "has_more", "next_offset",
            and "script_includes" list containing name, api_name, description,
            active, client_callable, access, and optionally the redacted script body.
        """
        logger.info("get_script_includes: name_filter=%s", name_filter)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if name_filter and "^" in name_filter:
            return _validation_error(
                "name_filter must not contain encoded query operators ('^').",
                "Provide a plain text search term without special characters.",
            )

        try:
            query_parts: list[str] = []
            if name_filter:
                query_parts.append(f"nameLIKE{name_filter}")
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts) if query_parts else ""

            records, total = await asyncio.gather(
                client.get_records(
                    table="sys_script_include",
                    query=query,
                    fields=[
                        "sys_id", "name", "api_name", "active",
                        "client_callable", "access", "description", "script",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYname",
                ),
                _safe_total(client, "sys_script_include", query),
            )

            includes = []
            for r in records:
                item: dict[str, Any] = {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "api_name": r.get("api_name", ""),
                    "active": r.get("active", ""),
                    "client_callable": r.get("client_callable", ""),
                    "access": r.get("access", ""),
                    "description": r.get("description", ""),
                }
                if include_scripts:
                    redacted = _redact_script_fields(r, ["script"])
                    item["script"] = redacted.get("script", "")
                includes.append(item)

            result: dict[str, Any] = {
                "count": len(includes),
                "script_includes": includes,
                "suggested_next": "Use get_business_rules(table) to see business rules, or get_client_scripts(table) for UI-side scripts.",
            }
            result.update(_pagination_metadata(total, offset, len(includes), limit))
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
    async def analyze_configurables(
        table: str,
    ) -> str:
        """Produce a summary of all configurables for a CMDB table.

        Counts business rules, client scripts, flows, ACLs, and script includes
        for the given table in a single overview. Uses the Aggregate API for
        efficient counting where possible, falling back to limited record fetches.

        Use this for a quick audit of what automation and access controls exist
        on a table before making changes.

        Args:
            table: The CMDB table to analyze (e.g. cmdb_ci_server).

        Returns:
            JSON object with "table" and counts for each configurable type:
            "business_rules", "client_scripts", "flows", "acls", "script_includes",
            each with "active_count" and "total_count".
        """
        logger.info("analyze_configurables: table=%s", table)
        if err := _require_client(client):
            return err

        if err := _validate_table_name(table):
            return _validation_error(err, "Provide a valid table name.", "Use suggest_table(description) to find the right table, or list_ci_classes() to browse.")

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
            except ServiceNowError as exc:
                return {
                    "_error": True,
                    "_error_category": exc.category,
                    "_retry": exc.retry,
                }

        results = await asyncio.gather(
            _safe_aggregate("sys_script", f"collection={table}"),
            _safe_aggregate("sys_script", f"collection={table}^active=true"),
            _safe_aggregate("sys_script_client", f"table={table}"),
            _safe_aggregate("sys_script_client", f"table={table}^active=true"),
            _safe_aggregate("sys_hub_flow", f"internal_nameCONTAINS{table}"),
            _safe_aggregate("sys_hub_flow", f"internal_nameCONTAINS{table}^active=true"),
            _safe_aggregate("sys_security_acl", f"nameSTARTSWITH{table}"),
            _safe_aggregate("sys_security_acl", f"nameSTARTSWITH{table}^active=true"),
            _safe_aggregate("sys_script_include", f"nameLIKE{table}"),
            _safe_aggregate("sys_script_include", f"nameLIKE{table}^active=true"),
        )

        def _category(total: dict, active: dict) -> dict[str, Any]:
            err = total if total.get("_error") else active if active.get("_error") else None
            if err:
                return {
                    "total_count": None,
                    "active_count": None,
                    "error": err.get("_error_category", "Unknown"),
                    "retry": err.get("_retry", False),
                }
            return {"total_count": _count(total), "active_count": _count(active)}

        return _json({
            "table": table,
            "business_rules": _category(results[0], results[1]),
            "client_scripts": _category(results[2], results[3]),
            "flows": _category(results[4], results[5]),
            "acls": _category(results[6], results[7]),
            "script_includes": _category(results[8], results[9]),
            "suggested_next": f"Use get_business_rules(table='{table}'), get_client_scripts(table='{table}'), get_acls(table='{table}'), get_flows(table='{table}'), or get_script_includes(name_filter='{table}') for details.",
        })
