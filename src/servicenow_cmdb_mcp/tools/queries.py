"""CI query tools — search, inspect, count, and schema discovery."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient, resolve_ref
from servicenow_cmdb_mcp.errors import NotFoundError, ServiceNowError

logger = logging.getLogger(__name__)

_MAX_LIMIT = 1000


def _clamp_limit(limit: int) -> int:
    """Clamp limit to valid range [1, 1000]."""
    return max(1, min(limit, _MAX_LIMIT))


def _clamp_offset(offset: int) -> int:
    """Clamp offset to non-negative."""
    return max(0, offset)


def _validate_table_name(table: str) -> str | None:
    """Validate table name is safe for URL interpolation. Returns error or None."""
    if not table or not table.strip():
        return "table must not be empty."
    if not all(c.isalnum() or c == "_" for c in table):
        return f"Invalid table name: '{table}'. Must contain only letters, digits, and underscores."
    return None


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


def _json(result: Any) -> str:
    """Serialize a result to JSON for tool responses."""
    return json.dumps(result, indent=2, default=str)


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


def register_query_tools(mcp: FastMCP, client: ServiceNowClient, cache: MetadataCache) -> None:
    """Register all CI query tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def search_cis(
        ci_class: str = "cmdb_ci",
        name_filter: str = "",
        operational_status: str = "",
        os_filter: str = "",
        location: str = "",
        limit: int = 25,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> str:
        """Search CMDB configuration items with structured filters.

        Builds an encoded query from the provided parameters and returns matching CIs.
        Name filtering uses STARTSWITH by default for performance (indexed operation).

        Args:
            ci_class: CMDB table/class to query (e.g. cmdb_ci_server, cmdb_ci_linux_server).
                      Defaults to cmdb_ci (all CI types).
            name_filter: Filter CIs whose name starts with this value. Leave empty for no name filter.
            operational_status: Filter by operational status value (e.g. "1" for Operational).
            os_filter: Filter by operating system (STARTSWITH match on the os field).
            location: Filter by location display value (STARTSWITH match).
            limit: Maximum number of results to return (1-1000, default 25).
            offset: Pagination offset for retrieving subsequent pages of results.
            fields: Specific fields to return. Defaults to sys_id, name, sys_class_name,
                    operational_status, ip_address, location, sys_updated_on.

        Returns:
            JSON object with "count" (number of results returned) and "records" (list of CI dicts).
        """
        logger.info("search_cis: class=%s name=%s", ci_class, name_filter)
        if err := _validate_table_name(ci_class):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
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
            records = await client.get_records(
                table=ci_class,
                query=query,
                fields=result_fields,
                limit=limit,
                offset=offset,
            )
            return _json({"count": len(records), "records": records})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def query_cis_raw(
        table: str,
        encoded_query: str,
        fields: list[str] | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Execute a raw encoded query against any CMDB table.

        For advanced users who know ServiceNow encoded query syntax. The query is passed
        directly to the Table API's sysparm_query parameter without modification.

        Examples of encoded queries:
        - "nameSTARTSWITHweb^operational_status=1" — operational CIs starting with "web"
        - "sys_class_name=cmdb_ci_linux_server^ip_addressISNOTEMPTY" — Linux servers with IPs
        - "sys_updated_on<javascript:gs.daysAgo(30)" — CIs not updated in 30 days

        Args:
            table: ServiceNow table name (e.g. cmdb_ci, cmdb_ci_server, cmdb_ci_win_server).
            encoded_query: Raw ServiceNow encoded query string.
            fields: Specific fields to return. Defaults to sys_id, name, sys_class_name,
                    operational_status, ip_address, location, sys_updated_on.
            limit: Maximum number of results to return (1-1000, default 25).
            offset: Pagination offset for retrieving subsequent pages.

        Returns:
            JSON object with "count" (number of results returned) and "records" (list of CI dicts).
        """
        logger.info("query_cis_raw: table=%s query=%s", table, encoded_query)
        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        result_fields = fields or _CI_LIST_FIELDS

        try:
            records = await client.get_records(
                table=table,
                query=encoded_query,
                fields=result_fields,
                limit=limit,
                offset=offset,
            )
            return _json({"count": len(records), "records": records})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_ci_details(
        sys_id: str,
        table: str = "cmdb_ci",
        fields: list[str] | None = None,
    ) -> str:
        """Get the full details of a single configuration item by its sys_id.

        Returns all requested fields for the CI. If no fields are specified, returns a broad
        set of common CI attributes. Use this tool when you need complete information about
        a specific CI, such as after finding it via search_cis.

        Args:
            sys_id: The sys_id of the CI record to retrieve.
            table: The CMDB table the CI belongs to (e.g. cmdb_ci_server). Defaults to cmdb_ci.
                   Using the specific class table is more efficient and returns class-specific fields.
            fields: Specific fields to return. If omitted, returns a broad default set including
                    sys_id, name, sys_class_name, asset_tag, serial_number, ip_address,
                    operational_status, install_status, location, department, company,
                    os, os_version, cpu_count, ram, disk_space, discovery_source,
                    first_discovered, last_discovered, sys_created_on, sys_updated_on.

        Returns:
            JSON object with the CI record, or an error if not found.
        """
        logger.info("get_ci_details: sys_id=%s table=%s", sys_id, table)
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
            )
            return _json(record)
        except NotFoundError:
            return _json({
                "error": True,
                "category": "NotFoundError",
                "message": f"No CI found with sys_id '{sys_id}' in table '{table}'",
                "suggestion": "Verify the sys_id and table name. The CI may exist in a different class table.",
                "retry": False,
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
    async def count_cis(
        table: str = "cmdb_ci",
        encoded_query: str = "",
        group_by: str = "",
    ) -> str:
        """Count configuration items matching a query using the Aggregate API.

        Uses the ServiceNow Stats API (/api/now/stats) for efficient counting without
        fetching actual records. Optionally group results by a field to get counts per value.

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
        if err := _validate_table_name(table):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })

        try:
            result = await client.get_aggregate(
                table=table,
                query=encoded_query,
                group_by=group_by,
            )
            return _json(result.get("result", result))
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def list_ci_classes(
        parent_class: str = "cmdb_ci",
        limit: int = 100,
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

        Returns:
            JSON object with "count" and "classes" (list of class dicts with name, label,
            parent class, and whether the class has children).
        """
        logger.info("list_ci_classes: parent=%s", parent_class)
        cache_key = f"ci_classes:{parent_class}"
        cached = cache.get(cache_key)
        if cached is not None:
            classes = cached[:limit]
            return _json({"count": len(classes), "classes": classes, "cached": True})

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
            sliced = classes[:limit]
            return _json({"count": len(sliced), "classes": sliced, "cached": False})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def describe_ci_class(
        class_name: str,
    ) -> str:
        """Get field definitions, descriptions, and suggested relationships for a CMDB class.

        Queries the sys_dictionary table for field metadata and cmdb_rel_type_suggest for
        relationship suggestions. Includes inherited fields from parent classes by walking the
        class hierarchy. Results are cached for 1 hour.

        Use this tool to understand the schema of a CI class before building queries,
        or to check what fields and relationships are available.

        Args:
            class_name: The CMDB class name to describe (e.g. cmdb_ci_server, cmdb_ci_linux_server).

        Returns:
            JSON object with "class_name", "fields" (list of field definitions with name, label,
            type, max_length, mandatory flag, and defining class), and "suggested_relationships"
            (list of relationship types suggested for this class).
        """
        logger.info("describe_ci_class: class=%s", class_name)
        cache_key = f"ci_class_desc:{class_name}"
        was_cached = cache.get(cache_key) is not None

        try:
            result = await fetch_class_description(client, cache, class_name)
            return _json({**result, "cached": was_cached})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def suggest_table(
        description: str,
    ) -> str:
        """Suggest the best CMDB table to query based on a natural language description.

        Given a description like "linux servers", "network switches", "web applications",
        or "load balancers", this tool searches the CMDB class hierarchy to find the most
        appropriate table. It queries sys_db_object and sys_documentation for class labels
        and descriptions to find the best match.

        Use this tool when you're unsure which CMDB table to query for a particular type
        of configuration item.

        Args:
            description: Natural language description of what you're looking for.
                        Examples: "linux servers", "network switches", "web applications",
                        "database instances", "storage devices", "virtual machines".

        Returns:
            JSON object with "suggestions" — a ranked list of matching CMDB tables with
            their name, label, and description. The first result is the best match.
        """
        logger.info("suggest_table: description=%s", description)

        # Extract keywords from the description for searching
        keywords = description.lower().split()

        # Filter keywords — skip very short words (articles, prepositions)
        filtered_keywords = [kw for kw in keywords if len(kw) >= 3]

        if not filtered_keywords:
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Description too short or vague to suggest a table.",
                "suggestion": "Provide a more specific description like 'linux servers' or 'network switches'.",
                "retry": False,
            })

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
                score = sum(1 for kw in filtered_keywords if kw in name or kw in label)
                if score > 0:
                    scored.append((score, cls))

            scored.sort(key=lambda x: x[0], reverse=True)
            suggestions = [item for _, item in scored[:10]]

            if not suggestions:
                return _json({
                    "suggestions": [],
                    "message": f"No CMDB tables found matching '{description}'. "
                    "Try broader terms or use list_ci_classes to browse available classes.",
                })

            return _json({
                "query": description,
                "suggestion_count": len(suggestions),
                "suggestions": suggestions,
            })
        except ServiceNowError as e:
            return e.to_json()
