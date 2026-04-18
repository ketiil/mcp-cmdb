"""CI query tools — search, inspect, count, and schema discovery."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient, resolve_ref
from servicenow_cmdb_mcp.errors import NotFoundError, ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _extract_agg_count,
    _json,
    _nav_url,
    _not_found_error,
    _pagination_metadata,
    _require_client,
    _safe_total,
    _validate_cmdb_table,
    _validate_sys_id,
    _validation_error,
)

logger = logging.getLogger(__name__)

# Valid operational_status values (string codes used in ServiceNow API)
VALID_OP_STATUS = {"1", "2", "3", "4", "5", "6", "7", "8"}

# Patterns blocked in raw encoded queries to prevent server-side script injection.
# ServiceNow evaluates javascript: expressions in queries — block user-supplied ones.
_DANGEROUS_QUERY_PATTERNS = re.compile(
    r"javascript:|gs\.(include|sleep|log|print|exec|eval|import)|"
    r"Packages\.|java\.|eval\(|new\s+Function",
    re.IGNORECASE,
)

# Default fields returned for CI list queries
_CI_LIST_FIELDS = [
    "sys_id",
    "name",
    "sys_class_name",
    "operational_status",
    "ip_address",
    "location",
    "sys_updated_on",
]


def _build_query_parts(parts: list[str]) -> str:
    """Join non-empty encoded query parts with ^."""
    return "^".join(p for p in parts if p)


async def fetch_class_description(
    client: ServiceNowClient, cache: MetadataCache, class_name: str
) -> dict[str, Any]:
    """Fetch field definitions and relationship suggestions for a CMDB class.

    Shared by the describe_ci_class tool and the cmdb://schema/classes/{class_name}
    resource. Results are cached under "ci_class_desc:{class_name}".

    Returns the result dict on success. Raises ServiceNowError on failure.
    """
    cache_key = f"ci_class_desc:{class_name}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Walk the class hierarchy to collect all ancestor class names.
    hierarchy = [class_name]
    current_name = class_name
    for _ in range(10):  # Guard against infinite loops
        class_records = await client.get_records(
            table="sys_db_object",
            query=f"name={current_name}",
            fields=["super_class"],
            limit=1,
        )
        if not class_records:
            break
        parent_sys_id = resolve_ref(class_records[0].get("super_class", ""))
        if not parent_sys_id:
            break
        try:
            parent_record = await client.get_record(
                table="sys_db_object",
                sys_id=parent_sys_id,
                fields=["name"],
            )
        except NotFoundError:
            break
        if not parent_record:
            break
        parent_name = parent_record.get("name", "")
        if not parent_name or parent_name in hierarchy:
            break
        hierarchy.append(parent_name)
        current_name = parent_name

    # Build OR query for all classes in the hierarchy.
    # Each OR branch must include elementISNOTEMPTY to avoid fetching
    # table-level records (rows with empty element).
    or_clauses = [f"name={cls}^elementISNOTEMPTY" for cls in hierarchy]
    dict_query = "^OR".join(or_clauses)

    # Fetch field definitions from sys_dictionary for the full hierarchy
    field_records = await client.get_records(
        table="sys_dictionary",
        query=dict_query,
        fields=[
            "name",
            "element",
            "column_label",
            "internal_type",
            "max_length",
            "mandatory",
            "reference",
            "default_value",
        ],
        limit=500,
        order_by="ORDERBYelement",
    )
    # Deduplicate: if a field appears in multiple classes in the hierarchy,
    # keep the most specific (child) definition. hierarchy[0] is the target class.
    seen: dict[str, dict[str, str | bool]] = {}
    for r in field_records:
        element = r.get("element", "")
        defining_class = r.get("name", "")
        if element not in seen:
            seen[element] = {
                "name": element,
                "label": r.get("column_label", ""),
                "type": r.get("internal_type", ""),
                "max_length": r.get("max_length", ""),
                "mandatory": r.get("mandatory", "false") == "true",
                "reference": r.get("reference", ""),
                "default_value": r.get("default_value", ""),
                "defined_on": defining_class,
            }
        elif defining_class in hierarchy:
            existing_class = str(seen[element].get("defined_on", ""))
            existing_idx = hierarchy.index(existing_class) if existing_class in hierarchy else len(hierarchy)
            new_idx = hierarchy.index(defining_class)
            if new_idx < existing_idx:
                seen[element] = {
                    "name": element,
                    "label": r.get("column_label", ""),
                    "type": r.get("internal_type", ""),
                    "max_length": r.get("max_length", ""),
                    "mandatory": r.get("mandatory", "false") == "true",
                    "reference": r.get("reference", ""),
                    "default_value": r.get("default_value", ""),
                    "defined_on": defining_class,
                }
    fields = sorted(seen.values(), key=lambda f: str(f["name"]))

    # Fetch suggested relationship types
    rel_records = await client.get_records(
        table="cmdb_rel_type_suggest",
        query=f"child_class_name={class_name}^ORparent_class_name={class_name}",
        fields=["rel_type", "parent_class_name", "child_class_name"],
        limit=50,
        order_by="ORDERBYrel_type",
    )
    relationships = [
        {
            "rel_type": r.get("rel_type", ""),
            "parent_class": r.get("parent_class_name", ""),
            "child_class": r.get("child_class_name", ""),
        }
        for r in rel_records
    ]

    result = {
        "class_name": class_name,
        "field_count": len(fields),
        "fields": fields,
        "suggested_relationships": relationships,
    }
    cache.set(cache_key, result)
    return result


def register_query_tools(mcp: FastMCP, client: ServiceNowClient | None, cache: MetadataCache) -> None:
    """Register all CI query tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def search_cis(
        ci_class: str = "cmdb_ci",
        name_filter: str = "",
        operational_status: Literal["", "1", "2", "3", "4", "5", "6", "7", "8"] = "",
        os_filter: str = "",
        location: str = "",
        limit: int = 25,
        offset: int = 0,
        fields: list[str] | None = None,
        display_value: Literal["", "true", "all"] = "",
    ) -> str:
        """Search CMDB configuration items with structured filters.

        Builds an encoded query from the provided parameters and returns matching CIs.
        Name filtering uses STARTSWITH by default for performance (indexed operation).

        Example: search_cis(ci_class="cmdb_ci_linux_server", name_filter="prod", operational_status="1")

        Typical workflow: suggest_table → search_cis → get_ci_details → get_ci_relationships

        Args:
            ci_class: CMDB table/class to query (e.g. cmdb_ci_server, cmdb_ci_linux_server).
                      Defaults to cmdb_ci (all CI types).
            name_filter: Filter CIs whose name starts with this value. Leave empty for no name filter.
            operational_status: Filter by operational status numeric code. Valid values:
                              "1" = Operational, "2" = Non-Operational, "3" = Repair in Progress,
                              "4" = DR Standby, "5" = Ready, "6" = Retired,
                              "7" = Pipeline, "8" = Catalog.
            os_filter: Filter by operating system (STARTSWITH match on the os field).
            location: Filter by location display value (STARTSWITH match).
            limit: Maximum number of results to return (1-1000, default 25).
            offset: Pagination offset for retrieving subsequent pages of results.
            fields: Specific fields to return. Defaults to sys_id, name, sys_class_name,
                    operational_status, ip_address, location, sys_updated_on.
            display_value: Controls how reference fields (location, company, assigned_to, etc.)
                          are returned. "" (default) returns raw sys_id values. "true" returns
                          human-readable display values (e.g. "New York" instead of a sys_id).
                          "all" returns both as {"value": sys_id, "display_value": "New York"}.
                          Use "true" when presenting data to users; use "all" when you need
                          both the sys_id (for API calls) and the display name.

        Returns:
            JSON object with "count" (number of results returned) and "records" (list of CI dicts).
        """
        logger.info("search_cis: class=%s name=%s", ci_class, name_filter)
        if err := _require_client(client):
            return err
        if err := _validate_cmdb_table(ci_class):
            return _validation_error(err, "Provide a valid CMDB table name (e.g. cmdb_ci_server).")
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        if operational_status and operational_status not in VALID_OP_STATUS:
            return _validation_error(
                f"Invalid operational_status '{operational_status}'. "
                "Valid values: 1=Operational, 2=Non-Operational, 3=Repair in Progress, "
                "4=DR Standby, 5=Ready, 6=Retired, 7=Pipeline, 8=Catalog.",
                "Use a numeric code from 1-8.",
            )
        if display_value and display_value not in ("true", "all"):
            return _validation_error(
                f"Invalid display_value '{display_value}'. Must be 'true', 'all', or omit.",
                "Use 'true' for display values only, 'all' for both sys_id and display value.",
            )
        query_parts: list[str] = []
        if name_filter:
            query_parts.append(f"nameSTARTSWITH{name_filter}")
        if operational_status:
            query_parts.append(f"operational_status={operational_status}")
        if os_filter:
            query_parts.append(f"osSTARTSWITH{os_filter}")
        if location:
            query_parts.append(f"location.nameSTARTSWITH{location}")

        query = _build_query_parts(query_parts)
        result_fields = fields or _CI_LIST_FIELDS

        try:
            records_coro = client.get_records(
                table=ci_class,
                query=query,
                fields=result_fields,
                limit=limit,
                offset=offset,
                display_value=display_value,
            )
            total_coro = _safe_total(client, ci_class, query)
            records, total = await asyncio.gather(records_coro, total_coro)

            for r in records:
                sid = r.get("sys_id", "")
                if sid:
                    r["url"] = _nav_url(client.base_url, ci_class, sid)

            result: dict[str, Any] = {
                "count": len(records),
                "records": records,
                "suggested_next": "Use get_ci_details(sys_id) for full record, get_ci_relationships(sys_id) for dependencies, or count_cis for totals.",
            }
            result.update(_pagination_metadata(total, offset, len(records), limit))
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
    async def query_cis_raw(
        table: str,
        encoded_query: str,
        fields: list[str] | None = None,
        limit: int = 25,
        offset: int = 0,
        display_value: Literal["", "true", "all"] = "",
    ) -> str:
        """Execute a raw encoded query against any CMDB table.

        For advanced users who know ServiceNow encoded query syntax. The query is passed
        directly to the Table API's sysparm_query parameter without modification.

        Note: Server-side script expressions (javascript:, gs.*, eval) are blocked
        for security. Use field-based operators only.

        Examples of encoded queries:
        - "nameSTARTSWITHweb^operational_status=1" — operational CIs starting with "web"
        - "sys_class_name=cmdb_ci_linux_server^ip_addressISNOTEMPTY" — Linux servers with IPs
        - "sys_updated_on<2025-01-01" — CIs not updated since a specific date

        Args:
            table: ServiceNow table name (e.g. cmdb_ci, cmdb_ci_server, cmdb_ci_win_server).
            encoded_query: Raw ServiceNow encoded query string.
            fields: Specific fields to return. Defaults to sys_id, name, sys_class_name,
                    operational_status, ip_address, location, sys_updated_on.
            limit: Maximum number of results to return (1-1000, default 25).
            offset: Pagination offset for retrieving subsequent pages.
            display_value: Controls how reference fields (location, company, assigned_to, etc.)
                          are returned. "" (default) returns raw sys_id values. "true" returns
                          human-readable display values (e.g. "New York" instead of a sys_id).
                          "all" returns both as {"value": sys_id, "display_value": "New York"}.
                          Use "true" when presenting data to users; use "all" when you need
                          both the sys_id (for API calls) and the display name.

        Returns:
            JSON object with "count" (number of results returned) and "records" (list of CI dicts).
        """
        logger.info("query_cis_raw: table=%s query=%s", table, encoded_query)
        if err := _require_client(client):
            return err
        if err := _validate_cmdb_table(table):
            return _validation_error(err, "Provide a valid CMDB table name (e.g. cmdb_ci_server).")
        if _DANGEROUS_QUERY_PATTERNS.search(encoded_query):
            return _validation_error(
                "Encoded query contains blocked patterns (javascript:, gs.*, eval, etc.).",
                "Use only field-based encoded query operators (=, STARTSWITH, IN, <, >, etc.). "
                "Server-side script expressions are not allowed in raw queries.",
            )
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        if display_value and display_value not in ("true", "all"):
            return _validation_error(
                f"Invalid display_value '{display_value}'. Must be 'true', 'all', or omit.",
                "Use 'true' for display values only, 'all' for both sys_id and display value.",
            )
        result_fields = fields or _CI_LIST_FIELDS

        try:
            records_coro = client.get_records(
                table=table,
                query=encoded_query,
                fields=result_fields,
                limit=limit,
                offset=offset,
                display_value=display_value,
            )
            total_coro = _safe_total(client, table, encoded_query)
            records, total = await asyncio.gather(records_coro, total_coro)

            for r in records:
                sid = r.get("sys_id", "")
                if sid:
                    r["url"] = _nav_url(client.base_url, table, sid)

            result: dict[str, Any] = {
                "count": len(records),
                "records": records,
                "suggested_next": "Use get_ci_details(sys_id) for full record, or get_ci_relationships(sys_id) for dependencies.",
            }
            result.update(_pagination_metadata(total, offset, len(records), limit))
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
    async def get_ci_details(
        sys_id: str,
        table: str = "cmdb_ci",
        fields: list[str] | None = None,
        display_value: Literal["", "true", "all"] = "",
    ) -> str:
        """Get the full details of a single configuration item by its sys_id.

        Returns all requested fields for the CI. If no fields are specified, returns a broad
        set of common CI attributes. Use this tool when you need complete information about
        a specific CI, such as after finding it via search_cis.

        Prerequisites: Use search_cis or query_cis_raw to find the sys_id first.
        This tool only accepts sys_id (a 32-character hex identifier), not CI names.
        To look up a CI by name: search_cis(name_filter="my-server") → use the returned sys_id.

        Args:
            sys_id: The 32-character sys_id of the CI record (from search_cis or query_cis_raw).
            table: The CMDB table the CI belongs to (e.g. cmdb_ci_server). Defaults to cmdb_ci.
                   Using the specific class table is more efficient and returns class-specific fields.
            fields: Specific fields to return. If omitted, returns a broad default set including
                    sys_id, name, sys_class_name, asset_tag, serial_number, ip_address,
                    operational_status, install_status, location, department, company,
                    os, os_version, cpu_count, ram, disk_space, discovery_source,
                    first_discovered, last_discovered, sys_created_on, sys_updated_on.
            display_value: Controls how reference fields (location, company, assigned_to, etc.)
                          are returned. "" (default) returns raw sys_id values. "true" returns
                          human-readable display values (e.g. "New York" instead of a sys_id).
                          "all" returns both as {"value": sys_id, "display_value": "New York"}.
                          Use "true" when presenting data to users; use "all" when you need
                          both the sys_id (for API calls) and the display name.

        Returns:
            JSON object with the CI record, or an error if not found.
        """
        logger.info("get_ci_details: sys_id=%s table=%s", sys_id, table)
        if err := _require_client(client):
            return err
        if err := _validate_cmdb_table(table):
            return _validation_error(err, "Provide a valid CMDB table name (e.g. cmdb_ci_server).")
        if err := _validate_sys_id(sys_id):
            return _validation_error(err, "Provide a valid CI sys_id.")
        if display_value and display_value not in ("true", "all"):
            return _validation_error(
                f"Invalid display_value '{display_value}'. Must be 'true', 'all', or omit.",
                "Use 'true' for display values only, 'all' for both sys_id and display value.",
            )
        default_fields = [
            "sys_id",
            "name",
            "sys_class_name",
            "asset_tag",
            "serial_number",
            "ip_address",
            "operational_status",
            "install_status",
            "location",
            "department",
            "company",
            "os",
            "os_version",
            "cpu_count",
            "ram",
            "disk_space",
            "discovery_source",
            "first_discovered",
            "last_discovered",
            "sys_created_on",
            "sys_updated_on",
        ]
        result_fields = fields or default_fields

        try:
            record = await client.get_record(
                table=table,
                sys_id=sys_id,
                fields=result_fields,
                display_value=display_value,
            )
            if not record:
                return _not_found_error(
                    f"No CI found with sys_id '{sys_id}' in table '{table}'",
                    "Verify the sys_id and table name. The CI may exist in a different class table.",
                )
            record["url"] = _nav_url(client.base_url, table, sys_id)
            record["suggested_next"] = "Use get_ci_relationships(sys_id) for dependencies, or preview_ci_update(sys_id, ...) to modify."
            return _json(record)
        except NotFoundError:
            return _not_found_error(
                f"No CI found with sys_id '{sys_id}' in table '{table}'",
                "Verify the sys_id and table name. The CI may exist in a different class table.",
            )
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def count_cis(
        table: str = "cmdb_ci",
        encoded_query: str = "",
        group_by: str = "",
    ) -> str:
        """Count configuration items matching a query using the Aggregate API.

        Uses the ServiceNow Stats API (/api/now/stats) for efficient counting without
        fetching actual records. Optionally group results by a field to get counts per value.

        Example: count_cis(table="cmdb_ci_linux_server", group_by="operational_status")

        Args:
            table: CMDB table to count records in (e.g. cmdb_ci_server). Defaults to cmdb_ci.
            encoded_query: Optional encoded query to filter which CIs to count.
                          Example: "operational_status=1" to count only operational CIs.
            group_by: Optional field name to group counts by (e.g. "sys_class_name" to get
                     counts per CI type, or "operational_status" for counts per status).

        Returns:
            JSON object with the aggregate count result. When group_by is used, returns
            counts broken down by each distinct value of that field.
        """
        logger.info("count_cis: table=%s query=%s group_by=%s", table, encoded_query, group_by)
        if err := _require_client(client):
            return err
        if err := _validate_cmdb_table(table):
            return _validation_error(err, "Provide a valid CMDB table name (e.g. cmdb_ci_server).")

        try:
            raw = await client.get_aggregate(
                table=table,
                query=encoded_query,
                group_by=group_by,
            )
            total = _extract_agg_count(raw)

            # "total" (not "total_count") because this IS the count tool —
            # the number is the primary result, not pagination metadata.
            result: dict[str, Any] = {
                "table": table,
                "total": total,
            }

            if group_by:
                # Parse grouped results from the Aggregate API response
                raw_result = raw.get("result", raw)
                groups: list[dict[str, Any]] = []
                if isinstance(raw_result, list):
                    for entry in raw_result:
                        group_value = ""
                        gb_fields = entry.get("groupby_fields", [])
                        if gb_fields and isinstance(gb_fields, list):
                            group_value = gb_fields[0].get("value", "")
                        try:
                            count = int(entry.get("stats", {}).get("count", 0))
                        except (ValueError, TypeError):
                            count = 0
                        groups.append({"value": group_value, "count": count})
                result["group_by"] = group_by
                result["groups"] = groups
                # Grouped responses don't have a top-level stats.count —
                # recompute total from individual group counts.
                result["total"] = sum(g["count"] for g in groups)

            result["suggested_next"] = (
                f"Use search_cis(ci_class='{table}') to retrieve the matching records, "
                "or query_cis_raw for complex filters."
            )
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
    async def list_ci_classes(
        parent_class: str = "cmdb_ci",
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """List available CMDB classes from the Data Model Navigator.

        Returns the class hierarchy under a given parent class. Results are cached for 1 hour
        to avoid repeated queries to the sys_db_object table.

        Use this tool to discover which CI classes exist in the instance before running queries,
        or to explore the CMDB class hierarchy.

        Args:
            parent_class: Parent class to list children of. Defaults to cmdb_ci (all CMDB classes).
                         Use a more specific parent like cmdb_ci_server to see only server subclasses.
            limit: Maximum number of classes to return (default 100).
            offset: Pagination offset for retrieving subsequent pages of results.

        Returns:
            JSON object with "count" and "classes" (list of class dicts with name, label,
            parent class, and whether the class has children).

        Note: offset-based pagination may shift if classes are added or removed between
        calls. For stable enumeration, fetch all classes in a single call with a higher limit.
        """
        logger.info("list_ci_classes: parent=%s", parent_class)
        if err := _require_client(client):
            return err
        offset = _clamp_offset(offset)
        cache_key = f"ci_classes:{parent_class}"
        cached = cache.get(cache_key)
        if cached is not None:
            sliced = cached[offset:offset + limit]
            return _json({
                "count": len(sliced),
                "total_count": len(cached),
                "has_more": offset + len(sliced) < len(cached),
                "next_offset": offset + len(sliced),
                "classes": sliced,
                "cached": True,
                "suggested_next": "Use describe_ci_class(class_name) for field definitions.",
            })

        try:
            records = await client.get_records(
                table="sys_db_object",
                query=f"super_class.name={parent_class}",
                fields=["name", "label", "super_class", "sys_id"],
                limit=500,
                order_by="ORDERBYname",
            )
            classes = [
                {
                    "name": r.get("name", ""),
                    "label": r.get("label", ""),
                    "parent": parent_class,
                    "sys_id": r.get("sys_id", ""),
                }
                for r in records
            ]
            cache.set(cache_key, classes)
            sliced = classes[offset:offset + limit]
            result: dict[str, Any] = {
                "count": len(sliced),
                "total_count": len(classes),
                "has_more": offset + len(sliced) < len(classes),
                "next_offset": offset + len(sliced),
                "classes": sliced,
                "cached": False,
                "suggested_next": "Use describe_ci_class(class_name) for field definitions.",
            }
            if len(records) == 500:
                result["truncated"] = True
                result["truncation_warning"] = (
                    "Results capped at 500 classes. Use a more specific parent_class "
                    "(e.g. cmdb_ci_server) to narrow results."
                )
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
    async def describe_ci_class(
        class_name: str,
        summary: bool = True,
    ) -> str:
        """Get field definitions, descriptions, and suggested relationships for a CMDB class.

        Queries the sys_dictionary table for field metadata and cmdb_rel_type_suggest for
        relationship suggestions. Includes inherited fields from parent classes by walking the
        class hierarchy. Results are cached for 1 hour.

        Use this tool to understand the schema of a CI class before building queries,
        or to check what fields and relationships are available.

        Prerequisites: Use list_ci_classes to find available class names, or suggest_table to find the right class.

        Args:
            class_name: The CMDB class name to describe (e.g. cmdb_ci_server, cmdb_ci_linux_server).
            summary: If True (default), return only field_count, mandatory_fields (names only),
                    and suggested_relationships — much lighter for initial exploration. Set False
                    to include the full fields list with all metadata.

        Returns:
            JSON object with "class_name", "field_count", and either a summary or the full
            "fields" list, plus "suggested_relationships".
        """
        logger.info("describe_ci_class: class=%s summary=%s", class_name, summary)
        if err := _require_client(client):
            return err

        try:
            full = await fetch_class_description(client, cache, class_name)
            if summary:
                mandatory = [
                    str(f["name"]) for f in full.get("fields", []) if f.get("mandatory")
                ]
                return _json({
                    "class_name": full["class_name"],
                    "field_count": full["field_count"],
                    "mandatory_fields": mandatory,
                    "suggested_relationships": full.get("suggested_relationships", []),
                    "suggested_next": f"Use describe_ci_class(class_name='{class_name}', summary=False) for full field definitions, or search_cis(ci_class='{class_name}') to query CIs.",
                })
            # Strip empty string values from field dicts to save tokens
            compact_fields = [
                {k: v for k, v in f.items() if v != ""}
                for f in full.get("fields", [])
            ]
            compact = {**full, "fields": compact_fields}
            return _json({**compact, "suggested_next": f"Use search_cis(ci_class='{class_name}') to query CIs of this class."})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def suggest_table(
        description: str,
        limit: int = 10,
        offset: int = 0,
    ) -> str:
        """Suggest the best CMDB table to query based on a natural language description.

        Given a description like "linux servers", "network switches", "web applications",
        or "load balancers", this tool searches the CMDB class hierarchy to find the most
        appropriate table. It queries sys_db_object and sys_documentation for class labels
        and descriptions to find the best match.

        Use this tool when you're unsure which CMDB table to query for a particular type
        of configuration item.

        Each suggestion includes a confidence score (0-100) indicating how well it matches
        the description. When a single result clearly dominates, it is marked as best_match.

        Args:
            description: Natural language description of what you're looking for.
                        Examples: "linux servers", "network switches", "web applications",
                        "database instances", "storage devices", "virtual machines".
            limit: Maximum number of suggestions to return (1-1000, default 10).
            offset: Pagination offset for retrieving subsequent pages of suggestions.

        Returns:
            JSON object with "suggestions" — a ranked list of matching CMDB tables with
            their name, label, confidence score, and pagination metadata.
        """
        logger.info("suggest_table: description=%s", description)
        if err := _require_client(client):
            return err

        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        # Extract keywords from the description for searching
        keywords = description.lower().split()

        # Filter keywords — skip very short words (articles, prepositions)
        filtered_keywords = [kw for kw in keywords if len(kw) >= 3]

        if not filtered_keywords:
            return _validation_error(
                "Description too short or vague to suggest a table.",
                "Provide a more specific description like 'linux servers' or 'network switches'.",
            )

        try:
            # Fetch all CMDB classes and score locally — more reliable than
            # building complex OR queries against sys_db_object
            cache_key = "suggest_table:all_classes"
            all_classes: list[dict[str, str]] | None = cache.get(cache_key)

            if all_classes is None:
                records = await client.get_records(
                    table="sys_db_object",
                    query="nameSTARTSWITHcmdb_ci",
                    fields=["name", "label", "super_class"],
                    limit=500,
                    order_by="ORDERBYname",
                )
                all_classes = [
                    {
                        "table": r.get("name", ""),
                        "label": r.get("label", ""),
                        "parent": r.get("super_class", ""),
                    }
                    for r in records
                ]
                cache.set(cache_key, all_classes)

            # Score each class by how many keywords appear in its name or label
            scored: list[tuple[int, dict[str, str]]] = []
            for cls in all_classes:
                name = cls["table"].lower()
                label = cls["label"].lower()
                raw_score = sum(1 for kw in filtered_keywords if kw in name or kw in label)
                if raw_score > 0:
                    scored.append((raw_score, cls))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Normalize scores to 0-100 confidence and annotate each suggestion
            keyword_count = len(filtered_keywords)
            all_suggestions: list[dict[str, Any]] = []
            for raw_score, cls in scored:
                confidence = int(100 * raw_score / keyword_count)
                suggestion = {**cls, "confidence": confidence}
                all_suggestions.append(suggestion)

            if not all_suggestions:
                return _json({
                    "suggestions": [],
                    "message": f"No CMDB tables found matching '{description}'. "
                    "Try broader terms or use list_ci_classes to browse available classes.",
                })

            # Mark best_match when the top result clearly dominates
            if len(all_suggestions) >= 2:
                top_conf = all_suggestions[0]["confidence"]
                second_conf = all_suggestions[1]["confidence"]
                if top_conf >= 80 and (top_conf - second_conf) >= 20:
                    all_suggestions[0]["best_match"] = True
            elif all_suggestions[0]["confidence"] >= 80:
                all_suggestions[0]["best_match"] = True

            total_count = len(all_suggestions)
            sliced = all_suggestions[offset:offset + limit]

            return _json({
                "query": description,
                "suggestion_count": len(sliced),
                "total_count": total_count,
                "has_more": offset + len(sliced) < total_count,
                "next_offset": offset + len(sliced),
                "suggestions": sliced,
            })
        except ServiceNowError as e:
            return e.to_json()
