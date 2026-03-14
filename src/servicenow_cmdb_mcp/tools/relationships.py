"""Relationship and dependency tools — traversal, impact, and discovery."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient, resolve_ref
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _json,
    _validate_sys_id,
)

logger = logging.getLogger(__name__)

# Fields to return for CIs referenced in relationships
_CI_REF_FIELDS = ["sys_id", "name", "sys_class_name", "operational_status"]

# Fields for relationship records
_REL_FIELDS = ["sys_id", "parent", "child", "type"]

# Maximum nodes to visit in BFS/DFS traversals to prevent unbounded memory
_MAX_TRAVERSAL_NODES = 500


async def _resolve_ci(client: ServiceNowClient, sys_id: str) -> dict[str, str]:
    """Resolve a CI sys_id to a summary dict with name and class."""
    if not sys_id:
        return {"sys_id": "", "name": "(unresolved)", "sys_class_name": "", "operational_status": ""}
    try:
        record = await client.get_record(
            table="cmdb_ci",
            sys_id=sys_id,
            fields=_CI_REF_FIELDS,
        )
        if record:
            return {
                "sys_id": record.get("sys_id", sys_id),
                "name": record.get("name", ""),
                "sys_class_name": record.get("sys_class_name", ""),
                "operational_status": record.get("operational_status", ""),
            }
    except ServiceNowError:
        pass
    return {"sys_id": sys_id, "name": "(unresolved)", "sys_class_name": "", "operational_status": ""}


async def _resolve_rel_type(client: ServiceNowClient, cache: MetadataCache, type_sys_id: str) -> dict[str, str]:
    """Resolve a relationship type sys_id to its descriptors, using cache."""
    if not type_sys_id:
        return {"sys_id": "", "name": "(unresolved)", "parent_descriptor": "", "child_descriptor": ""}
    cache_key = f"rel_type:{type_sys_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        record = await client.get_record(
            table="cmdb_rel_type",
            sys_id=type_sys_id,
            fields=["sys_id", "name", "parent_descriptor", "child_descriptor"],
        )
        if record:
            result = {
                "sys_id": record.get("sys_id", type_sys_id),
                "name": record.get("name", ""),
                "parent_descriptor": record.get("parent_descriptor", ""),
                "child_descriptor": record.get("child_descriptor", ""),
            }
            cache.set(cache_key, result)
            return result
    except ServiceNowError:
        pass
    return {"sys_id": type_sys_id, "name": "(unresolved)", "parent_descriptor": "", "child_descriptor": ""}


async def _fetch_relationships(
    client: ServiceNowClient,
    cache: MetadataCache,
    ci_sys_id: str,
    direction: str,
    rel_type_sys_id: str = "",
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch and resolve relationships for a CI.

    When direction is "both", the limit is split evenly between upstream and
    downstream queries so the total result count respects the caller's limit.
    Offset is applied per-direction (each direction has its own result set).

    Args:
        direction: "upstream" (CI is child), "downstream" (CI is parent), or "both".
        rel_type_sys_id: Optional relationship type sys_id to filter by.
        limit: Total max results to return across all directions.
        offset: Pagination offset applied per direction.
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    results: list[dict[str, Any]] = []

    queries: list[tuple[str, str]] = []  # (query, perspective)
    if direction in ("upstream", "both"):
        q = f"child={ci_sys_id}"
        if rel_type_sys_id:
            q += f"^type={rel_type_sys_id}"
        queries.append((q, "upstream"))
    if direction in ("downstream", "both"):
        q = f"parent={ci_sys_id}"
        if rel_type_sys_id:
            q += f"^type={rel_type_sys_id}"
        queries.append((q, "downstream"))

    # Split limit across directions so total doesn't exceed caller's limit
    per_direction_limit = limit // len(queries) if queries else limit

    # Collect all relationship records first, then batch-resolve CIs
    all_records: list[tuple[dict[str, Any], str]] = []  # (record, perspective)
    for query, perspective in queries:
        records = await client.get_records(
            table="cmdb_rel_ci",
            query=query,
            fields=_REL_FIELDS,
            limit=per_direction_limit,
            offset=offset,
        )
        for r in records:
            all_records.append((r, perspective))

    # Batch-resolve related CI names in one query instead of N individual calls
    related_ids: set[str] = set()
    for r, perspective in all_records:
        parent_id = resolve_ref(r.get("parent", ""))
        child_id = resolve_ref(r.get("child", ""))
        related_id = parent_id if perspective == "upstream" else child_id
        if related_id:
            related_ids.add(related_id)

    ci_map: dict[str, dict[str, str]] = {}
    if related_ids:
        ids_list = list(related_ids)
        for i in range(0, len(ids_list), 100):
            batch = ids_list[i:i + 100]
            batch_str = ",".join(batch)
            ci_records = await client.get_records(
                table="cmdb_ci",
                query=f"sys_idIN{batch_str}",
                fields=_CI_REF_FIELDS,
                limit=len(batch),
            )
            for ci in ci_records:
                sid = ci.get("sys_id", "")
                ci_map[sid] = {
                    "sys_id": sid,
                    "name": ci.get("name", ""),
                    "sys_class_name": ci.get("sys_class_name", ""),
                    "operational_status": ci.get("operational_status", ""),
                }

    # Build results using the batch-resolved data
    for r, perspective in all_records:
        parent_id = resolve_ref(r.get("parent", ""))
        child_id = resolve_ref(r.get("child", ""))
        type_id = resolve_ref(r.get("type", ""))

        related_id = parent_id if perspective == "upstream" else child_id
        related_ci = ci_map.get(related_id, {
            "sys_id": related_id, "name": "(unresolved)",
            "sys_class_name": "", "operational_status": "",
        })
        rel_type = await _resolve_rel_type(client, cache, type_id)

        results.append({
            "relationship_sys_id": r.get("sys_id", ""),
            "direction": perspective,
            "related_ci": related_ci,
            "relationship_type": rel_type,
        })

    return results


def register_relationship_tools(mcp: FastMCP, client: ServiceNowClient, cache: MetadataCache) -> None:
    """Register all relationship and dependency tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def get_ci_relationships(
        ci_sys_id: str,
        direction: str = "both",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Get all relationships for a configuration item.

        Returns upstream (CIs this CI depends on), downstream (CIs that depend on this CI),
        or both. Each relationship includes the related CI's name, class, and operational
        status, plus the relationship type descriptor.

        In ServiceNow CMDB relationships:
        - Upstream: this CI is the CHILD in the relationship (e.g., "Runs on" a server)
        - Downstream: this CI is the PARENT (e.g., a server that other CIs "Run on")

        Args:
            ci_sys_id: The sys_id of the CI to get relationships for.
            direction: Which relationships to return: "upstream", "downstream", or "both".
                      Defaults to "both".
            limit: Maximum relationships to return per direction (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "ci_sys_id", "direction", "count", and "relationships" list.
        """
        logger.info("get_ci_relationships: ci=%s direction=%s", ci_sys_id, direction)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_sys_id(ci_sys_id):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CI sys_id.",
                "retry": False,
            })

        if direction not in ("upstream", "downstream", "both"):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Invalid direction '{direction}'. Must be 'upstream', 'downstream', or 'both'.",
                "suggestion": "Use 'upstream' for dependencies, 'downstream' for dependents, or 'both'.",
                "retry": False,
            })

        try:
            relationships = await _fetch_relationships(
                client, cache, ci_sys_id, direction, limit=limit, offset=offset,
            )
            return _json({
                "ci_sys_id": ci_sys_id,
                "direction": direction,
                "count": len(relationships),
                "relationships": relationships,
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
    async def get_dependency_tree(
        ci_sys_id: str,
        direction: str = "upstream",
        max_depth: int = 3,
        limit_per_level: int = 10,
    ) -> str:
        """Walk the dependency tree from a CI with configurable depth and direction.

        Recursively traverses relationships to build a tree of dependencies. Useful for
        understanding the full dependency chain of a CI — what it runs on, what runs on it,
        or both.

        Args:
            ci_sys_id: The sys_id of the starting CI.
            direction: Direction to traverse: "upstream" (what this CI depends on) or
                      "downstream" (what depends on this CI). Defaults to "upstream".
            max_depth: How many levels deep to traverse (1-5, default 3). Higher values
                      make more API calls. Capped at 5 to prevent runaway traversals.
            limit_per_level: Maximum CIs to follow at each level (default 10). Controls
                            breadth of the tree to avoid excessive API calls.

        Returns:
            JSON tree structure with "ci" (root CI details), "depth", "direction", and
            "children" (nested list of related CIs, each with their own "children").
        """
        logger.info("get_dependency_tree: ci=%s direction=%s depth=%d", ci_sys_id, direction, max_depth)

        if err := _validate_sys_id(ci_sys_id):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CI sys_id.",
                "retry": False,
            })

        if direction not in ("upstream", "downstream"):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Invalid direction '{direction}'. Must be 'upstream' or 'downstream'.",
                "suggestion": "Use 'upstream' to see what this CI depends on, 'downstream' to see what depends on it.",
                "retry": False,
            })

        max_depth = max(1, min(max_depth, 5))
        visited: set[str] = set()

        async def walk(sys_id: str, depth: int) -> dict[str, Any]:
            ci_info = await _resolve_ci(client, sys_id)
            node: dict[str, Any] = {"ci": ci_info, "children": []}

            if sys_id in visited or depth >= max_depth or len(visited) >= _MAX_TRAVERSAL_NODES:
                return node
            visited.add(sys_id)

            try:
                rels = await _fetch_relationships(
                    client, cache, sys_id, direction, limit=limit_per_level,
                )
                for rel in rels:
                    child_id = rel["related_ci"]["sys_id"]
                    if child_id not in visited:
                        child_node = await walk(child_id, depth + 1)
                        child_node["relationship_type"] = rel["relationship_type"]
                        node["children"].append(child_node)
            except ServiceNowError:
                pass  # Partial tree is still useful

            return node

        try:
            tree = await walk(ci_sys_id, 0)
            return _json({
                "direction": direction,
                "max_depth": max_depth,
                "tree": tree,
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
    async def list_relationship_types(
        limit: int = 50,
    ) -> str:
        """List all relationship types available in the ServiceNow instance.

        Returns relationship types from cmdb_rel_type with their parent and child
        descriptors. For example, the "Runs on::Runs" type has parent_descriptor
        "Runs on" and child_descriptor "Runs". Results are cached for 1 hour.

        Args:
            limit: Maximum number of relationship types to return (default 50).

        Returns:
            JSON object with "count" and "relationship_types" list containing
            sys_id, name, parent_descriptor, and child_descriptor for each type.
        """
        logger.info("list_relationship_types")
        limit = _clamp_limit(limit)
        cache_key = "rel_types:all"
        cached = cache.get(cache_key)
        if cached is not None:
            sliced = cached[:limit]
            return _json({"count": len(sliced), "relationship_types": sliced, "cached": True})

        try:
            records = await client.get_records(
                table="cmdb_rel_type",
                fields=["sys_id", "name", "parent_descriptor", "child_descriptor"],
                limit=200,
                order_by="ORDERBYname",
            )
            rel_types = [
                {
                    "sys_id": r.get("sys_id", ""),
                    "name": r.get("name", ""),
                    "parent_descriptor": r.get("parent_descriptor", ""),
                    "child_descriptor": r.get("child_descriptor", ""),
                }
                for r in records
            ]
            cache.set(cache_key, rel_types)
            sliced = rel_types[:limit]
            return _json({"count": len(sliced), "relationship_types": sliced, "cached": False})
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def find_related_cis(
        ci_sys_id: str,
        rel_type: str,
        direction: str = "both",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Find CIs related to a given CI by a specific relationship type.

        Filters relationships to only those matching the specified type. The rel_type
        can be a sys_id or a relationship type name (e.g., "Runs on::Runs"). If a name
        is provided, it is resolved to a sys_id first.

        Args:
            ci_sys_id: The sys_id of the CI to find related CIs for.
            rel_type: Relationship type — either a sys_id or a name like "Runs on::Runs".
            direction: "upstream", "downstream", or "both". Defaults to "both".
            limit: Maximum results to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "ci_sys_id", "rel_type", "direction", "count", and
            "relationships" list.
        """
        logger.info("find_related_cis: ci=%s type=%s direction=%s", ci_sys_id, rel_type, direction)
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_sys_id(ci_sys_id):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CI sys_id.",
                "retry": False,
            })

        if direction not in ("upstream", "downstream", "both"):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Invalid direction '{direction}'.",
                "suggestion": "Use 'upstream', 'downstream', or 'both'.",
                "retry": False,
            })

        try:
            # Resolve rel_type name to sys_id if it doesn't look like a sys_id
            rel_type_sys_id = rel_type
            if len(rel_type) != 32 or not rel_type.isalnum():
                # Sanitize: reject encoded query operators and excessive length
                if "^" in rel_type or len(rel_type) > 200:
                    return _json({
                        "error": True,
                        "category": "ValidationError",
                        "message": "Relationship type name contains invalid characters.",
                        "suggestion": "Use list_relationship_types to see available type names.",
                        "retry": False,
                    })
                # Looks like a name — resolve it
                type_records = await client.get_records(
                    table="cmdb_rel_type",
                    query=f"name={rel_type}",
                    fields=["sys_id", "name"],
                    limit=1,
                )
                if not type_records:
                    return _json({
                        "error": True,
                        "category": "NotFoundError",
                        "message": f"Relationship type '{rel_type}' not found.",
                        "suggestion": "Use list_relationship_types to see available types.",
                        "retry": False,
                    })
                rel_type_sys_id = type_records[0].get("sys_id", "")

            relationships = await _fetch_relationships(
                client, cache, ci_sys_id, direction,
                rel_type_sys_id=rel_type_sys_id,
                limit=limit, offset=offset,
            )
            return _json({
                "ci_sys_id": ci_sys_id,
                "rel_type": rel_type,
                "direction": direction,
                "count": len(relationships),
                "relationships": relationships,
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
    async def get_impact_summary(
        ci_sys_id: str,
        max_depth: int = 3,
    ) -> str:
        """Produce a summary of services and applications impacted by a given CI.

        Traverses downstream relationships (CIs that depend on this CI) up to the
        specified depth, then categorizes the impacted CIs by class. Focuses on
        business-relevant classes: business applications, services, and application
        clusters.

        Use this tool for change impact assessment — understanding what would be
        affected if this CI goes down.

        Args:
            ci_sys_id: The sys_id of the CI to assess impact for.
            max_depth: How deep to traverse downstream dependencies (1-5, default 3).

        Returns:
            JSON object with "ci" (the source CI), "total_impacted" count,
            "impacted_by_class" (breakdown by CI class), and "impacted_services"
            (list of business apps/services found in the tree).
        """
        logger.info("get_impact_summary: ci=%s depth=%d", ci_sys_id, max_depth)

        if err := _validate_sys_id(ci_sys_id):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CI sys_id.",
                "retry": False,
            })

        max_depth = max(1, min(max_depth, 5))

        # Classes considered business-critical for impact reporting
        service_classes = {
            "cmdb_ci_service",
            "cmdb_ci_service_auto",
            "cmdb_ci_service_discovered",
            "cmdb_ci_business_app",
            "cmdb_ci_appl",
            "cmdb_ci_application_cluster",
            "cmdb_ci_service_group",
        }

        visited: set[str] = set()
        seen_impacted: set[str] = {ci_sys_id}  # Exclude root CI from its own impact list
        all_impacted: list[dict[str, str]] = []
        impacted_services: list[dict[str, str]] = []

        async def collect(sys_id: str, depth: int) -> None:
            if depth >= max_depth or sys_id in visited or len(visited) >= _MAX_TRAVERSAL_NODES:
                return
            visited.add(sys_id)

            try:
                rels = await _fetch_relationships(
                    client, cache, sys_id, "downstream", limit=50,
                )
                for rel in rels:
                    related = rel["related_ci"]
                    related_id = related["sys_id"]
                    if related_id not in seen_impacted:
                        seen_impacted.add(related_id)
                        all_impacted.append(related)
                        if related.get("sys_class_name", "") in service_classes:
                            impacted_services.append(related)
                    if related_id not in visited:
                        await collect(related_id, depth + 1)
            except ServiceNowError:
                pass

        try:
            source_ci = await _resolve_ci(client, ci_sys_id)
            await collect(ci_sys_id, 0)

            # Group impacted CIs by class
            by_class: dict[str, int] = {}
            for ci in all_impacted:
                cls = ci.get("sys_class_name", "unknown")
                by_class[cls] = by_class.get(cls, 0) + 1

            return _json({
                "ci": source_ci,
                "total_impacted": len(all_impacted),
                "impacted_by_class": by_class,
                "impacted_services": impacted_services,
                "traversal_depth": max_depth,
            })
        except ServiceNowError as e:
            return e.to_json()
