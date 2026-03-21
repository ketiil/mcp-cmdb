"""IRE inspection tools — identification rules, reconciliation rules, duplicate explanation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _has_more,
    _json,
    _nav_url,
    _require_client,
    _safe_total,
    _validate_cmdb_table,
    _validate_sys_id,
    _validate_table_name,
)

logger = logging.getLogger(__name__)


def register_ire_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all IRE inspection tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_identification_rules(
        table: str = "",
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get CMDB Identification and Reconciliation Engine (IRE) identification rules.

        Identification rules define how incoming data is matched to existing CIs.
        Each rule specifies which fields (identifiers) are used to uniquely identify
        a CI of a given class. When discovery or import finds a CI, these rules
        determine whether to create a new record or update an existing one.

        Args:
            table: Filter by CI class table name (e.g. cmdb_ci_server). Optional —
                   omit to list rules for all classes.
            active_only: If True, return only active rules. Defaults to True.
            limit: Maximum rules to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "identification_rules" list containing
            name, applies_to table, identifier fields, priority, and active status.
        """
        logger.info("get_identification_rules: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if table:
            if err := _validate_table_name(table):
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": err, "suggestion": "Provide a valid table name.",
                    "retry": False,
                })

        try:
            query_parts: list[str] = []
            if table:
                query_parts.append(f"applies_to={table}")
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="cmdb_ident_entry",
                    query=query,
                    fields=[
                        "sys_id", "name", "applies_to", "active",
                        "identifiers", "priority", "description",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYpriority",
                ),
                _safe_total(client, "cmdb_ident_entry", query),
            )

            rules = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "applies_to": r.get("applies_to", ""),
                    "active": r.get("active", ""),
                    "identifiers": r.get("identifiers", ""),
                    "priority": r.get("priority", ""),
                    "description": r.get("description", ""),
                }
                for r in records
            ]

            result: dict[str, Any] = {
                "count": len(rules),
                "table_filter": table or "(all)",
                "identification_rules": rules,
                "suggested_next": "Use get_reconciliation_rules(table) to see data refresh rules, or explain_duplicate(sys_id_a, sys_id_b) to compare two CIs against these rules.",
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
    async def get_reconciliation_rules(
        table: str = "",
        active_only: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get CMDB IRE reconciliation (data refresh) rules.

        Reconciliation rules control how field values are updated when multiple
        data sources provide conflicting values for the same CI. They define
        source priorities and which source "wins" for each field, preventing
        lower-priority sources from overwriting authoritative data.

        Args:
            table: Filter by CI class table name (e.g. cmdb_ci_server). Optional —
                   omit to list rules for all classes.
            active_only: If True, return only active rules. Defaults to True.
            limit: Maximum rules to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "count" and "reconciliation_rules" list containing
            name, applies_to table, source, priority, attributes, and active status.
        """
        logger.info("get_reconciliation_rules: table=%s", table)
        if err := _require_client(client):
            return err
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if table:
            if err := _validate_table_name(table):
                return _json({
                    "error": True, "category": "ValidationError",
                    "message": err, "suggestion": "Provide a valid table name.",
                    "retry": False,
                })

        try:
            query_parts: list[str] = []
            if table:
                query_parts.append(f"applies_to={table}")
            if active_only:
                query_parts.append("active=true")
            query = "^".join(query_parts)

            records, total = await asyncio.gather(
                client.get_records(
                    table="cmdb_reconciliation_rule",
                    query=query,
                    fields=[
                        "sys_id", "name", "applies_to", "active",
                        "source", "priority", "attributes", "description",
                    ],
                    limit=limit,
                    offset=offset,
                    order_by="ORDERBYpriority",
                ),
                _safe_total(client, "cmdb_reconciliation_rule", query),
            )

            rules = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "applies_to": r.get("applies_to", ""),
                    "active": r.get("active", ""),
                    "source": r.get("source", ""),
                    "priority": r.get("priority", ""),
                    "attributes": r.get("attributes", ""),
                    "description": r.get("description", ""),
                }
                for r in records
            ]

            result: dict[str, Any] = {
                "count": len(rules),
                "table_filter": table or "(all)",
                "reconciliation_rules": rules,
                "suggested_next": "Use get_identification_rules(table) to see matching rules, or find_duplicate_cis(table) to find potential duplicates.",
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
    async def explain_duplicate(
        sys_id_a: str,
        sys_id_b: str,
        table: str = "cmdb_ci",
    ) -> str:
        """Explain why two CIs might be duplicates by comparing their identification fields.

        Fetches both CIs and the identification rules for their class, then
        compares the identifier fields side-by-side. This helps determine whether
        two CIs are true duplicates or false positives, and which identification
        rule matched (or failed to match).

        Prerequisites: Use find_duplicate_cis to identify the duplicate pair first.

        Example: explain_duplicate(sys_id_a="abc123...", sys_id_b="def456...", table="cmdb_ci_server")

        Args:
            sys_id_a: sys_id of the first CI.
            sys_id_b: sys_id of the second CI.
            table: The CMDB table both CIs belong to (default cmdb_ci).

        Returns:
            JSON object with both CIs' key fields, the applicable identification
            rules, and a field-by-field comparison showing matches and mismatches.
        """
        logger.info("explain_duplicate: a=%s… b=%s… table=%s", sys_id_a[:8], sys_id_b[:8], table)
        if err := _require_client(client):
            return err

        if err := _validate_sys_id(sys_id_a):
            return _json({
                "error": True, "category": "ValidationError",
                "message": f"sys_id_a: {err}",
                "suggestion": "Provide the sys_id of the first CI.",
                "retry": False,
            })

        if err := _validate_sys_id(sys_id_b):
            return _json({
                "error": True, "category": "ValidationError",
                "message": f"sys_id_b: {err}",
                "suggestion": "Provide the sys_id of the second CI.",
                "retry": False,
            })

        if sys_id_a == sys_id_b:
            return _json({
                "error": True, "category": "ValidationError",
                "message": "sys_id_a and sys_id_b are the same. Provide two different CIs to compare.",
                "suggestion": "Use two distinct sys_ids.",
                "retry": False,
            })

        if err := _validate_cmdb_table(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })

        try:
            # First fetch identification rules to know which fields to compare
            try:
                ident_rules = await client.get_records(
                    table="cmdb_ident_entry",
                    query=f"applies_to={table}^active=true",
                    fields=["sys_id", "name", "applies_to", "identifiers", "priority"],
                    limit=10,
                    order_by="ORDERBYpriority",
                )
            except ServiceNowError:
                # IRE tables may not exist — continue without rules
                ident_rules = []

            # Extract identifier field names from rules
            all_ident_fields: set[str] = set()
            rule_summaries: list[dict[str, Any]] = []
            for rule in ident_rules:
                idents_raw = rule.get("identifiers", "")
                # identifiers field is typically comma-separated field names
                ident_fields = [f.strip() for f in idents_raw.split(",") if f.strip()] if idents_raw else []
                all_ident_fields.update(ident_fields)
                rule_summaries.append({
                    "name": rule.get("name", ""),
                    "priority": rule.get("priority", ""),
                    "identifier_fields": ident_fields,
                })

            # Determine which fields to fetch and compare
            common_fields = {"name", "serial_number", "sys_class_name", "ip_address"}
            compare_fields = sorted(all_ident_fields | common_fields)
            fetch_fields = compare_fields + ["sys_id"]

            # Fetch both CIs in parallel with specific fields
            ci_a, ci_b = await asyncio.gather(
                client.get_record(table=table, sys_id=sys_id_a, fields=fetch_fields),
                client.get_record(table=table, sys_id=sys_id_b, fields=fetch_fields),
            )

            if not ci_a:
                return _json({
                    "error": True, "category": "NotFoundError",
                    "message": f"CI A not found: sys_id '{sys_id_a}' in table '{table}'.",
                    "suggestion": "Verify the sys_id and table.",
                    "retry": False,
                })

            if not ci_b:
                return _json({
                    "error": True, "category": "NotFoundError",
                    "message": f"CI B not found: sys_id '{sys_id_b}' in table '{table}'.",
                    "suggestion": "Verify the sys_id and table.",
                    "retry": False,
                })

            comparison: list[dict[str, Any]] = []
            for field in compare_fields:
                val_a = str(ci_a.get(field, ""))
                val_b = str(ci_b.get(field, ""))
                comparison.append({
                    "field": field,
                    "ci_a_value": val_a,
                    "ci_b_value": val_b,
                    "match": val_a == val_b and val_a != "",
                    "is_identifier": field in all_ident_fields,
                })

            matching_ident_fields = [c for c in comparison if c["is_identifier"] and c["match"]]
            mismatched_ident_fields = [c for c in comparison if c["is_identifier"] and not c["match"]]

            return _json({
                "ci_a": {
                    "sys_id": sys_id_a,
                    "name": ci_a.get("name", ""),
                    "sys_class_name": ci_a.get("sys_class_name", ""),
                    "url": _nav_url(client.base_url, table, sys_id_a),
                },
                "ci_b": {
                    "sys_id": sys_id_b,
                    "name": ci_b.get("name", ""),
                    "sys_class_name": ci_b.get("sys_class_name", ""),
                    "url": _nav_url(client.base_url, table, sys_id_b),
                },
                "table": table,
                "identification_rules": rule_summaries,
                "field_comparison": comparison,
                "summary": {
                    "matching_identifiers": len(matching_ident_fields),
                    "mismatched_identifiers": len(mismatched_ident_fields),
                    "total_identifier_fields": len(all_ident_fields),
                    "likely_duplicate": len(matching_ident_fields) > 0 and len(mismatched_ident_fields) == 0,
                },
                "suggested_next": f"If likely_duplicate=true, use preview_ci_update to correct identifier fields, or review in ServiceNow CI Remediation Workspace. Use get_reconciliation_rules(table='{table}') to check which data source should be authoritative.",
            })
        except ServiceNowError as e:
            return e.to_json()
