"""Relationship and dependency tools — traversal, impact, and discovery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient, resolve_ref
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._tree_format import render_ascii_tree
from servicenow_cmdb_mcp.tools._utils import (
    _clamp_limit,
    _clamp_offset,
    _extract_agg_count,
    _json,
    _nav_url,
    _not_found_error,
    _pagination_metadata,
    _require_client,
    _validate_sys_id,
    _validation_error,
)

logger = logging.getLogger(__name__)

# Maximum wall-clock seconds for recursive traversals (dependency tree, impact)
_TRAVERSAL_TIMEOUT = 60.0

# Fields to return for CIs referenced in relationships
_CI_REF_FIELDS = ["sys_id", "name", "sys_class_name", "operational_status"]

# Fields for relationship records
_REL_FIELDS = ["sys_id", "parent", "child", "type"]

# Maximum nodes to visit in BFS/DFS traversals to prevent unbounded memory
_MAX_TRAVERSAL_NODES = 500


async def _safe_rel_total(
    client: ServiceNowClient, ci_sys_id: str, direction: str, rel_type_sys_id: str = "",
) -> int | None:
    """Count total relationships for a CI via aggregate, returning None on failure."""
    try:
        queries: list[str] = []
        if direction in ("upstream", "both"):
            q = f"child={ci_sys_id}"
            if rel_type_sys_id:
                q += f"^type={rel_type_sys_id}"
            queries.append(q)
        if direction in ("downstream", "both"):
            q = f"parent={ci_sys_id}"
            if rel_type_sys_id:
                q += f"^type={rel_type_sys_id}"
            queries.append(q)
        agg_results = await asyncio.gather(
            *(client.get_aggregate(table="cmdb_rel_ci", query=q) for q in queries)
        )
        return sum(_extract_agg_count(agg) for agg in agg_results)
    except ServiceNowError:
        return None


async def _resolve_ci(client: ServiceNowClient, sys_id: str) -> dict[str, str]:
    """Resolve a CI sys_id to a summary dict with name and class."""
    if not sys_id:
        return {"sys_id": "", "name": "(unresolved)", "sys_class_name": "", "operational_status": "", "url": ""}
    try:
        record = await client.get_record(
            table="cmdb_ci",
            sys_id=sys_id,
            fields=_CI_REF_FIELDS,
        )
        if record:
            cls = record.get("sys_class_name", "cmdb_ci")
            return {
                "sys_id": record.get("sys_id", sys_id),
                "name": record.get("name", ""),
                "sys_class_name": cls,
                "operational_status": record.get("operational_status", ""),
                "url": _nav_url(client.base_url, cls or "cmdb_ci", sys_id),
            }
    except ServiceNowError:
        pass
    return {"sys_id": sys_id, "name": "(unresolved)", "sys_class_name": "", "operational_status": "", "url": ""}


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
                cls = ci.get("sys_class_name", "cmdb_ci")
                ci_map[sid] = {
                    "sys_id": sid,
                    "name": ci.get("name", ""),
                    "sys_class_name": cls,
                    "operational_status": ci.get("operational_status", ""),
                    "url": _nav_url(client.base_url, cls or "cmdb_ci", sid),
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


def register_relationship_tools(mcp: FastMCP, client: ServiceNowClient | None, cache: MetadataCache) -> None:
    """Register all relationship and dependency tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def get_ci_relationships(
        ci_sys_id: str,
        direction: Literal["upstream", "downstream", "both"] = "both",
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

        Prerequisites: Use search_cis to find the CI sys_id first. This tool only accepts
        sys_id (a 32-character hex identifier), not CI names. To look up a CI by name:
        search_cis(name_filter="my-server") → use the returned sys_id.

        Example: get_ci_relationships(ci_sys_id="abc123...", direction="downstream", limit=10)

        Args:
            ci_sys_id: The 32-character sys_id of the CI (from search_cis or query_cis_raw).
            direction: Which relationships to return: "upstream", "downstream", or "both".
                      Defaults to "both".
            limit: Maximum relationships to return per direction (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "ci_sys_id", "direction", "count", and "relationships" list.
        """
        logger.info("get_ci_relationships: ci=%s direction=%s", ci_sys_id, direction)
        if err := _require_client(client):
            return err
        assert client is not None
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_sys_id(ci_sys_id):
            return _validation_error(err, "Provide a valid CI sys_id.")

        if direction not in ("upstream", "downstream", "both"):
            return _validation_error(
                f"Invalid direction '{direction}'. Must be 'upstream', 'downstream', or 'both'.",
                "Use 'upstream' for dependencies, 'downstream' for dependents, or 'both'.",
            )

        try:
            relationships, total = await asyncio.gather(
                _fetch_relationships(
                    client, cache, ci_sys_id, direction, limit=limit, offset=offset,
                ),
                _safe_rel_total(client, ci_sys_id, direction),
            )
            result: dict[str, Any] = {
                "ci_sys_id": ci_sys_id,
                "direction": direction,
                "count": len(relationships),
                "relationships": relationships,
                "suggested_next": "Use get_dependency_tree(sys_id) for full dependency chain, or get_impact_summary(sys_id) for service impact.",
            }
            result.update(_pagination_metadata(total, offset, len(relationships), limit))
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
    async def get_dependency_tree(
        ci_sys_id: str,
        direction: Literal["upstream", "downstream"] = "upstream",
        max_depth: int = 3,
        limit_per_level: int = 10,
        class_filter: list[str] | None = None,
        format: Literal["json", "ascii_tree"] = "json",
    ) -> str:
        """Walk the dependency tree from a CI with configurable depth and direction.

        Recursively traverses relationships to build a tree of dependencies. Useful for
        understanding the full dependency chain of a CI — what it runs on, what runs on it.

        Prerequisites: Use search_cis to find the root CI sys_id first.

        Performance: API calls grow exponentially with max_depth — depth=3 with
        limit_per_level=10 can issue up to ~111 calls. Start with max_depth=2
        and increase only if needed. Reduce limit_per_level for wide graphs.
        A hard 60-second timeout applies; on timeout only the root node is
        returned (in-progress subtrees are discarded) with timed_out=true.


        Example: get_dependency_tree(ci_sys_id="abc123...", direction="downstream", max_depth=2)

        Args:
            ci_sys_id: The sys_id of the starting CI (32-character hex string from search_cis).
            direction: Direction to traverse: "upstream" (what this CI depends on) or
                      "downstream" (what depends on this CI). Defaults to "upstream".
            max_depth: How many levels deep to traverse (1-5, default 3). Higher values
                      make more API calls. Capped at 5 to prevent runaway traversals.
            limit_per_level: Maximum CIs to follow at each level (default 10). Controls
                            breadth of the tree to avoid excessive API calls.
            class_filter: Optional list of sys_class_name values to include in the tree.
                         Only CIs matching these classes appear in the output. CIs that
                         don't match are still traversed (their children may match), but
                         they are collapsed out of the result. When None or empty, all
                         classes are included. Example: ["cmdb_ci_server", "cmdb_ci_linux_server"].
            format: Output format. "json" (default) returns the nested tree structure.
                   "ascii_tree" returns a pre-rendered text tree — much smaller, readable
                   without post-processing, but loses sys_id and status detail.

        Returns:
            JSON tree structure with "ci" (root CI details), "depth", "direction", and
            "children" (nested list of related CIs, each with their own "children").
        """
        logger.info("get_dependency_tree: ci=%s direction=%s depth=%d", ci_sys_id, direction, max_depth)
        if err := _require_client(client):
            return err
        assert client is not None

        if err := _validate_sys_id(ci_sys_id):
            return _validation_error(err, "Provide a valid CI sys_id.")

        if direction not in ("upstream", "downstream"):
            return _validation_error(
                f"Invalid direction '{direction}'. Must be 'upstream' or 'downstream'.",
                "Use 'upstream' to see what this CI depends on, 'downstream' to see what depends on it.",
            )

        max_depth = max(1, min(max_depth, 5))
        filter_set = set(class_filter) if class_filter else set()
        visited: set[str] = set()
        traversal_errors: list[str] = []
        # Mutable node registry so partial trees survive timeout cancellation.
        # Each node is registered as soon as it is created; child links are
        # appended in-place, so the root node always reflects whatever was
        # completed before the deadline.
        nodes: dict[str, dict[str, Any]] = {}

        async def walk(sys_id: str, depth: int) -> dict[str, Any]:
            ci_info = await _resolve_ci(client, sys_id)
            node: dict[str, Any] = {"ci": ci_info, "children": []}
            nodes[sys_id] = node

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
                        if filter_set:
                            child_cls = child_node["ci"].get("sys_class_name", "")
                            if child_cls in filter_set or child_node["children"]:
                                node["children"].append(child_node)
                        else:
                            node["children"].append(child_node)
            except ServiceNowError as e:
                traversal_errors.append(f"Node {sys_id}: {e.message}")

            return node

        try:
            timed_out = False
            # Pre-register root node so timeout handler never needs a network call.
            root_ci = await _resolve_ci(client, ci_sys_id)
            nodes[ci_sys_id] = {"ci": root_ci, "children": []}
            try:
                tree = await asyncio.wait_for(walk(ci_sys_id, 0), timeout=_TRAVERSAL_TIMEOUT)
            except asyncio.TimeoutError:
                timed_out = True
                # Return the partial tree accumulated before timeout.
                # The root node was pre-registered so this never fails.
                tree = nodes[ci_sys_id]
                traversal_errors.append(
                    f"Traversal timed out after {_TRAVERSAL_TIMEOUT}s. "
                    "Partial tree returned — some subtrees may be incomplete. "
                    "Try reducing max_depth or limit_per_level."
                )

            result: dict[str, Any] = {
                "direction": direction,
                "max_depth": max_depth,
                "nodes_visited": len(visited),
                "tree": tree,
                "suggested_next": "Use get_impact_summary(sys_id) for service impact, or get_ci_details(sys_id) to inspect a specific node.",
            }
            if filter_set:
                result["class_filter"] = sorted(filter_set)
            if timed_out:
                result["timed_out"] = True
            if traversal_errors:
                result["is_partial"] = True
                result["traversal_errors"] = traversal_errors
            if format == "ascii_tree":
                ascii_result: dict[str, Any] = {
                    "direction": direction,
                    "max_depth": max_depth,
                    "nodes_visited": len(visited),
                    "tree_text": render_ascii_tree(tree),
                    "suggested_next": result.get("suggested_next", ""),
                }
                if filter_set:
                    ascii_result["class_filter"] = sorted(filter_set)
                if timed_out:
                    ascii_result["timed_out"] = True
                if traversal_errors:
                    ascii_result["is_partial"] = True
                    ascii_result["traversal_errors"] = traversal_errors
                return _json(ascii_result)
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
    async def list_relationship_types(
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List all relationship types available in the ServiceNow instance.

        Returns relationship types from cmdb_rel_type with their parent and child
        descriptors. For example, the "Runs on::Runs" type has parent_descriptor
        "Runs on" and child_descriptor "Runs". Results are cached for 1 hour.

        Args:
            limit: Maximum number of relationship types to return (default 50).
            offset: Pagination offset for retrieving subsequent pages of results.

        Returns:
            JSON object with "count" and "relationship_types" list containing
            sys_id, name, parent_descriptor, and child_descriptor for each type.

        Typical workflow: list_relationship_types → find_related_cis(ci_sys_id, rel_type=sys_id)
        """
        logger.info("list_relationship_types")
        if err := _require_client(client):
            return err
        assert client is not None
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        cache_key = "rel_types:all"
        cached = cache.get(cache_key)
        if cached is not None:
            sliced = cached[offset:offset + limit]
            return _json({
                "count": len(sliced),
                "total_count": len(cached),
                "has_more": offset + len(sliced) < len(cached),
                "next_offset": offset + len(sliced),
                "relationship_types": sliced,
                "cached": True,
                "suggested_next": "Use find_related_cis(ci_sys_id, rel_type=<type_sys_id>) to find CIs with a specific relationship type.",
            })

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
            sliced = rel_types[offset:offset + limit]
            result: dict[str, Any] = {
                "count": len(sliced),
                "total_count": len(rel_types),
                "has_more": offset + len(sliced) < len(rel_types),
                "next_offset": offset + len(sliced),
                "relationship_types": sliced,
                "cached": False,
                "suggested_next": "Use find_related_cis(ci_sys_id, rel_type=<type_sys_id>) to find CIs with a specific relationship type.",
            }
            if len(records) == 200:
                result["truncated"] = True
                result["truncation_warning"] = (
                    "Results capped at 200 relationship types. "
                    "The instance may have more types than shown."
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
    async def find_related_cis(
        ci_sys_id: str,
        rel_type: str,
        direction: Literal["upstream", "downstream", "both"] = "both",
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
        if err := _require_client(client):
            return err
        assert client is not None
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        if err := _validate_sys_id(ci_sys_id):
            return _validation_error(err, "Provide a valid CI sys_id.")

        if direction not in ("upstream", "downstream", "both"):
            return _validation_error(
                f"Invalid direction '{direction}'.",
                "Use 'upstream', 'downstream', or 'both'.",
            )

        try:
            # Resolve rel_type name to sys_id if it doesn't look like a sys_id
            rel_type_sys_id = rel_type
            if len(rel_type) != 32 or not rel_type.isalnum():
                # Sanitize: reject encoded query operators and excessive length
                if "^" in rel_type or len(rel_type) > 200:
                    return _validation_error(
                        "Relationship type name contains invalid characters.",
                        "Use list_relationship_types to see available type names.",
                    )
                # Looks like a name — resolve it
                type_records = await client.get_records(
                    table="cmdb_rel_type",
                    query=f"name={rel_type}",
                    fields=["sys_id", "name"],
                    limit=1,
                )
                if not type_records:
                    return _not_found_error(
                        f"Relationship type '{rel_type}' not found.",
                        "Use list_relationship_types to see available types.",
                    )
                rel_type_sys_id = type_records[0].get("sys_id", "")

            relationships, total = await asyncio.gather(
                _fetch_relationships(
                    client, cache, ci_sys_id, direction,
                    rel_type_sys_id=rel_type_sys_id,
                    limit=limit, offset=offset,
                ),
                _safe_rel_total(client, ci_sys_id, direction, rel_type_sys_id),
            )
            result: dict[str, Any] = {
                "ci_sys_id": ci_sys_id,
                "rel_type": rel_type,
                "direction": direction,
                "count": len(relationships),
                "relationships": relationships,
                "suggested_next": "Use get_ci_details(sys_id) to inspect a related CI, or get_dependency_tree(sys_id) for the full chain.",
            }
            result.update(_pagination_metadata(total, offset, len(relationships), limit))
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
    async def get_impact_summary(
        ci_sys_id: str,
        max_depth: int = 3,
        class_filter: list[str] | None = None,
    ) -> str:
        """Produce a summary of services and applications impacted by a given CI.

        Traverses downstream relationships (CIs that depend on this CI) up to the
        specified depth, then categorizes the impacted CIs by class. Focuses on
        business-relevant classes: business applications, services, and application
        clusters. Also useful for blast radius analysis, change risk assessment,
        and understanding service dependencies before scheduled maintenance.

        Use this tool for change impact assessment — understanding what would be
        affected if this CI goes down.

        Prerequisites: Use search_cis to find the CI sys_id first.

        Performance: Traversal can issue many API calls for deeply connected CIs.
        Consider using max_depth=2 for initial assessment, then increasing if
        needed. A hard 60-second timeout applies; on timeout, impact counts
        reflect only what was traversed before the deadline (timed_out=true).

        Args:
            ci_sys_id: The sys_id of the CI to assess impact for (32-character hex string
                      from search_cis).
            max_depth: How deep to traverse downstream dependencies (1-5, default 3).
            class_filter: Optional list of sys_class_name values to include in impact
                         counts and results. Traversal still visits all CIs (to find
                         matching descendants), but only matching classes appear in
                         totals and lists. When None or empty, all classes are included.

        Returns:
            JSON object with "ci" (the source CI), "total_impacted" count,
            "impacted_by_class" (breakdown by CI class), and "impacted_services"
            (list of business apps/services found in the tree).
        """
        logger.info("get_impact_summary: ci=%s depth=%d", ci_sys_id, max_depth)
        if err := _require_client(client):
            return err
        assert client is not None

        if err := _validate_sys_id(ci_sys_id):
            return _validation_error(err, "Provide a valid CI sys_id.")

        max_depth = max(1, min(max_depth, 5))
        filter_set = set(class_filter) if class_filter else set()

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
        traversal_errors: list[str] = []

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
                        related_cls = related.get("sys_class_name", "")
                        if not filter_set or related_cls in filter_set:
                            all_impacted.append(related)
                            if related_cls in service_classes:
                                impacted_services.append(related)
                    if related_id not in visited:
                        await collect(related_id, depth + 1)
            except ServiceNowError as e:
                traversal_errors.append(f"Node {sys_id}: {e.message}")

        try:
            source_ci = await _resolve_ci(client, ci_sys_id)
            timed_out = False
            try:
                await asyncio.wait_for(collect(ci_sys_id, 0), timeout=_TRAVERSAL_TIMEOUT)
            except asyncio.TimeoutError:
                timed_out = True
                traversal_errors.append(
                    f"Traversal timed out after {_TRAVERSAL_TIMEOUT}s. "
                    "Try reducing max_depth."
                )

            # Group impacted CIs by class
            by_class: dict[str, int] = {}
            for ci in all_impacted:
                cls = ci.get("sys_class_name", "unknown")
                by_class[cls] = by_class.get(cls, 0) + 1

            traversal_complete = not timed_out and not traversal_errors
            result: dict[str, Any] = {
                "ci": source_ci,
                "total_impacted": len(all_impacted),
                "traversal_complete": traversal_complete,
                "impacted_by_class": by_class,
                "impacted_services": impacted_services,
                "traversal_depth": max_depth,
                "nodes_visited": len(visited),
                "suggested_next": "Use get_ci_details(sys_id) to inspect impacted CIs, or get_dependency_tree(sys_id) to trace the full chain.",
            }
            if timed_out:
                result["timed_out"] = True
            if traversal_errors:
                result["is_partial"] = True
                result["traversal_errors"] = traversal_errors
            if filter_set:
                result["class_filter"] = sorted(filter_set)
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
    async def find_ci_path(
        source_sys_id: str,
        target_sys_id: str,
        max_depth: int = 5,
    ) -> str:
        """Find the shortest relationship path between two CIs.

        Performs a bidirectional BFS (both upstream and downstream at each hop)
        to find the shortest chain of relationships connecting two CIs. Useful
        when you know both endpoints and want to understand how they are related
        without traversing the full tree.

        Prerequisites: Use search_cis to find both CI sys_ids first.

        Performance: BFS explores breadth-first with a limit of 10 relationships
        per node per direction. A hard 60-second timeout applies.

        Example: find_ci_path(source_sys_id="abc...", target_sys_id="def...", max_depth=4)

        Args:
            source_sys_id: The sys_id of the starting CI.
            target_sys_id: The sys_id of the target CI to find a path to.
            max_depth: Maximum hops to search (1-10, default 5). Higher values
                      find longer paths but issue more API calls.

        Returns:
            JSON object with "found" (bool), "path" (ordered list of nodes from
            source to target, each with "ci" details and "relationship_type"),
            and "hops" (number of relationships in the path).
        """
        logger.info("find_ci_path: source=%s target=%s depth=%d", source_sys_id, target_sys_id, max_depth)
        if err := _require_client(client):
            return err
        assert client is not None

        for label, sid in [("source_sys_id", source_sys_id), ("target_sys_id", target_sys_id)]:
            if err := _validate_sys_id(sid):
                return _validation_error(f"Invalid {label}: {err}", "Provide a valid CI sys_id.")

        max_depth = max(1, min(max_depth, 10))

        if source_sys_id == target_sys_id:
            ci_info = await _resolve_ci(client, source_sys_id)
            return _json({
                "found": True,
                "hops": 0,
                "path": [{"ci": ci_info}],
                "suggested_next": "Source and target are the same CI.",
            })

        from collections import deque

        visited: set[str] = {source_sys_id}
        queue: deque[tuple[str, list[tuple[str, dict[str, str] | None]]]] = deque()
        queue.append((source_sys_id, [(source_sys_id, None)]))

        try:
            timed_out = False

            async def bfs() -> list[tuple[str, dict[str, str] | None]] | None:
                while queue:
                    current_id, path = queue.popleft()
                    if len(path) > max_depth + 1:
                        continue

                    rels = await _fetch_relationships(
                        client, cache, current_id, "both", limit=10,
                    )
                    for rel in rels:
                        neighbor_id = rel["related_ci"]["sys_id"]
                        if neighbor_id in visited:
                            continue
                        rel_type = rel["relationship_type"]
                        new_path = path + [(neighbor_id, rel_type)]
                        if neighbor_id == target_sys_id:
                            return new_path
                        visited.add(neighbor_id)
                        if len(new_path) <= max_depth + 1:
                            queue.append((neighbor_id, new_path))
                return None

            try:
                found_path = await asyncio.wait_for(bfs(), timeout=_TRAVERSAL_TIMEOUT)
            except asyncio.TimeoutError:
                timed_out = True
                found_path = None

            if found_path:
                path_nodes: list[dict[str, Any]] = []
                for sys_id, rel_type in found_path:
                    ci_info = await _resolve_ci(client, sys_id)
                    node: dict[str, Any] = {"ci": ci_info}
                    if rel_type:
                        node["relationship_type"] = rel_type
                    path_nodes.append(node)

                return _json({
                    "found": True,
                    "hops": len(found_path) - 1,
                    "path": path_nodes,
                    "nodes_visited": len(visited),
                    "suggested_next": "Use get_ci_details(sys_id) to inspect any node, or get_dependency_tree(sys_id) for the full tree.",
                })
            else:
                result: dict[str, Any] = {
                    "found": False,
                    "hops": 0,
                    "path": [],
                    "nodes_visited": len(visited),
                    "max_depth_searched": max_depth,
                    "suggested_next": "Try increasing max_depth, or the CIs may not be connected.",
                }
                if timed_out:
                    result["timed_out"] = True
                    result["message"] = f"Search timed out after {_TRAVERSAL_TIMEOUT}s. Try reducing max_depth."
                return _json(result)
        except ServiceNowError as e:
            return e.to_json()
