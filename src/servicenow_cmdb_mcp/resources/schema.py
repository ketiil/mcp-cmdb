"""Dynamic MCP Resources from the Data Model Navigator and instance metadata."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import _require_client, _validate_table_name
from servicenow_cmdb_mcp.tools.queries import fetch_class_description

logger = logging.getLogger(__name__)


def _json(result: Any) -> str:
    return json.dumps(result, indent=2, default=str)


async def _fetch_all_classes(
    client: ServiceNowClient, cache: MetadataCache
) -> dict[str, Any]:
    """Fetch all CMDB classes as a flat list with resolved parent class names.

    Fetches sys_id to build a local lookup, then resolves super_class
    references to actual class names (not display labels).

    Uses cache.get_or_fetch for stampede protection — concurrent callers
    coalesce into a single API call.

    Returns dict with "classes" list and "truncated" flag.
    """

    async def _do_fetch() -> dict[str, Any]:
        response = await client.get(
            "/api/now/table/sys_db_object",
            params={
                "sysparm_query": "nameSTARTSWITHcmdb_ci^ORDERBYname",
                "sysparm_fields": "sys_id,name,label,super_class",
                "sysparm_limit": "1000",
            },
        )
        records = response.get("result", [])

        # Build sys_id → class name lookup for resolving parent references
        id_to_name: dict[str, str] = {}
        for r in records:
            sid = r.get("sys_id", "")
            name = r.get("name", "")
            if sid and name:
                id_to_name[sid] = name

        classes = []
        for r in records:
            parent_ref = r.get("super_class", "")
            parent_id = parent_ref.get("value", "") if isinstance(parent_ref, dict) else str(parent_ref)
            parent_name = id_to_name.get(parent_id, parent_id)
            classes.append({
                "name": r.get("name", ""),
                "label": r.get("label", ""),
                "parent": parent_name,
            })

        return {"classes": classes, "truncated": len(records) >= 1000}

    return await cache.get_or_fetch("resource:classes", _do_fetch)


async def _fetch_relationship_types(
    client: ServiceNowClient, cache: MetadataCache
) -> list[dict[str, str]]:
    """Fetch all relationship types. Shares cache key with list_relationship_types tool."""

    async def _do_fetch() -> list[dict[str, str]]:
        records = await client.get_records(
            table="cmdb_rel_type",
            fields=["sys_id", "name", "parent_descriptor", "child_descriptor"],
            limit=200,
            order_by="ORDERBYname",
        )
        return [
            {
                "sys_id": r.get("sys_id", ""),
                "name": r.get("name", ""),
                "parent_descriptor": r.get("parent_descriptor", ""),
                "child_descriptor": r.get("child_descriptor", ""),
            }
            for r in records
        ]

    return await cache.get_or_fetch("rel_types:all", _do_fetch)


async def _safe_get_records(
    client: ServiceNowClient, label: str, **kwargs: Any
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch records with graceful error handling for asyncio.gather arms."""
    try:
        return await client.get_records(**kwargs), None
    except ServiceNowError as e:
        logger.warning("Instance metadata fetch failed for %s: %s", label, e.message)
        return [], f"{label}: {e.message}"


async def _safe_get_aggregate(
    client: ServiceNowClient, label: str, **kwargs: Any
) -> tuple[dict[str, Any], str | None]:
    """Fetch aggregate with graceful error handling for asyncio.gather arms."""
    try:
        return await client.get_aggregate(**kwargs), None
    except ServiceNowError as e:
        logger.warning("Instance metadata fetch failed for %s: %s", label, e.message)
        return {}, f"{label}: {e.message}"


async def _fetch_instance_metadata(
    client: ServiceNowClient, cache: MetadataCache
) -> dict[str, Any]:
    """Fetch instance version, CMDB plugins, and CI count."""

    async def _do_fetch() -> dict[str, Any]:
        return await _do_fetch_instance_metadata(client)

    return await cache.get_or_fetch("resource:instance_metadata", _do_fetch)


async def _do_fetch_instance_metadata(
    client: ServiceNowClient,
) -> dict[str, Any]:
    """Inner fetch for instance metadata — called under cache lock."""
    (props, props_err), (plugins, plugins_err), (agg, agg_err) = await asyncio.gather(
        _safe_get_records(
            client,
            "sys_properties",
            table="sys_properties",
            query="name=glide.buildname^ORname=glide.war",
            fields=["name", "value"],
            limit=10,
        ),
        _safe_get_records(
            client,
            "v_plugin",
            table="v_plugin",
            query="active=true^nameSTARTSWITHCMDB",
            fields=["name", "version", "active"],
            limit=50,
            order_by="ORDERBYname",
        ),
        _safe_get_aggregate(
            client,
            "cmdb_ci_count",
            table="cmdb_ci",
        ),
    )

    # Extract version — prefer glide.buildname, fall back to glide.war
    version = ""
    for p in props:
        if p.get("name") == "glide.buildname":
            version = p.get("value", "")
            break
    if not version:
        for p in props:
            if p.get("name") == "glide.war":
                version = p.get("value", "")
                break

    # Extract CI count from aggregate response
    ci_count = 0
    try:
        stats = agg.get("result", {})
        if isinstance(stats, dict):
            ci_count = int(stats.get("stats", {}).get("count", 0))
        elif isinstance(stats, list) and stats:
            ci_count = int(stats[0].get("stats", {}).get("count", 0))
    except (ValueError, TypeError, IndexError):
        pass

    # Assemble plugin list
    cmdb_plugins = [
        {
            "name": p.get("name", ""),
            "version": p.get("version", ""),
        }
        for p in plugins
    ]

    # Collect errors from any failed arms
    errors = [e for e in [props_err, plugins_err, agg_err] if e is not None]

    result: dict[str, Any] = {
        "version": version,
        "ci_count": ci_count,
        "cmdb_plugins": cmdb_plugins,
        "plugin_count": len(cmdb_plugins),
    }
    if errors:
        result["errors"] = errors

    return result


def register_schema_resources(
    mcp: FastMCP, client: ServiceNowClient, cache: MetadataCache
) -> None:
    """Register MCP Resources and the refresh_metadata_cache utility tool."""

    @mcp.resource(
        "cmdb://schema/classes",
        name="CMDB Class Hierarchy",
        description="Flat list of all CMDB CI classes with name, label, and parent class.",
        mime_type="application/json",
    )
    async def cmdb_schema_classes() -> str:
        """Full CMDB class hierarchy as a flat list from sys_db_object."""
        logger.info("resource: cmdb://schema/classes")
        if err := _require_client(client):
            return err
        try:
            data = await _fetch_all_classes(client, cache)
            classes = data["classes"]
            result: dict[str, Any] = {"count": len(classes), "classes": classes}
            if data["truncated"]:
                result["truncated"] = True
                result["warning"] = "Result capped at 1000. Instance may have more CI classes."
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.resource(
        "cmdb://schema/classes/{class_name}",
        name="CMDB Class Fields",
        description="Field definitions, types, and suggested relationships for a specific CMDB class.",
        mime_type="application/json",
    )
    async def cmdb_schema_class_fields(class_name: str) -> str:
        """Field definitions for a specific CMDB CI class from sys_dictionary."""
        logger.info("resource: cmdb://schema/classes/%s", class_name)
        if err := _require_client(client):
            return err

        if err := _validate_table_name(class_name):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err, "suggestion": "Provide a valid class name.",
                "retry": False,
            })

        try:
            result = await fetch_class_description(client, cache, class_name)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.resource(
        "cmdb://schema/relationship-types",
        name="CMDB Relationship Types",
        description="All relationship types available in the CMDB with parent/child descriptors.",
        mime_type="application/json",
    )
    async def cmdb_schema_relationship_types() -> str:
        """All relationship types from cmdb_rel_type."""
        logger.info("resource: cmdb://schema/relationship-types")
        if err := _require_client(client):
            return err
        try:
            types = await _fetch_relationship_types(client, cache)
            return _json({"count": len(types), "relationship_types": types})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.resource(
        "cmdb://instance/metadata",
        name="Instance Metadata",
        description="ServiceNow instance version, installed CMDB plugins, and total CI count.",
        mime_type="application/json",
    )
    async def cmdb_instance_metadata() -> str:
        """Instance version, CMDB plugins, and CI count."""
        logger.info("resource: cmdb://instance/metadata")
        if err := _require_client(client):
            return err
        try:
            result = await _fetch_instance_metadata(client, cache)
            return _json(result)
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def refresh_metadata_cache() -> str:
        """Clear the in-memory metadata cache, forcing fresh data on next access.

        Clears all cached schema data including class hierarchies, field definitions,
        relationship types, and instance metadata. The next tool or resource access
        will re-fetch from ServiceNow automatically.

        Use this after making schema changes in ServiceNow (e.g., adding fields,
        creating new CI classes, or modifying relationship types) to ensure the
        MCP server reflects the latest state.

        Returns:
            JSON object confirming the cache was cleared.
        """
        logger.info("refresh_metadata_cache: clearing all cached metadata")
        cache.clear()
        return _json({
            "cleared": True,
            "message": "Metadata cache cleared. Next resource or tool access will fetch fresh data.",
        })
