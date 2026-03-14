"""CMDB health tools — orphan detection, duplicate finding, staleness, and health summary."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient, resolve_ref
from servicenow_cmdb_mcp.errors import ServiceNowError
from servicenow_cmdb_mcp.tools._utils import (
    _MAX_LIMIT,
    _clamp_limit,
    _clamp_offset,
    _json,
    _validate_cmdb_table,
)

logger = logging.getLogger(__name__)

# Default fields for health-related CI listings
_HEALTH_CI_FIELDS = [
    "sys_id",
    "name",
    "sys_class_name",
    "operational_status",
    "sys_updated_on",
    "discovery_source",
]


def _extract_count(agg_result: dict[str, Any]) -> int:
    """Extract the integer count from an Aggregate API response."""
    result = agg_result.get("result", agg_result)
    if isinstance(result, dict):
        stats = result.get("stats", {})
        try:
            return int(stats.get("count", 0))
        except (ValueError, TypeError):
            return 0
    return 0


def _parse_agg_groups(agg_result: dict[str, Any], empty_label: str = "unknown") -> dict[str, int]:
    """Parse grouped Aggregate API results into a {value: count} dict."""
    raw = agg_result.get("result", [])
    if isinstance(raw, dict):
        raw = [raw] if raw else []
    groups: dict[str, int] = {}
    for group in raw:
        stats = group.get("stats", {})
        try:
            count = int(stats.get("count", 0))
        except (ValueError, TypeError):
            count = 0
        group_fields = group.get("groupby_fields", [{}])
        if isinstance(group_fields, list) and group_fields:
            val = group_fields[0].get("value", "") or empty_label
        else:
            val = empty_label
        groups[val] = count
    return groups


def register_health_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all CMDB health tools on the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def find_orphan_cis(
        ci_class: str = "cmdb_ci",
        operational_status: str = "",
        limit: int = 25,
        scan_offset: int = 0,
    ) -> str:
        """Find CIs that have no relationships in cmdb_rel_ci.

        Orphan CIs are configuration items with zero upstream or downstream
        relationships. These often indicate incomplete discovery, manual entries
        that were never linked, or leftover records from decommissioned infrastructure.

        Scans a batch of CIs and checks each against cmdb_rel_ci using the
        Aggregate API. Results may be partial if the orphan ratio is low — use
        scan_offset to continue scanning from where the previous call left off.

        Args:
            ci_class: CMDB table to search for orphans (e.g. cmdb_ci_server).
                      Defaults to cmdb_ci (all types).
            operational_status: Optional filter by operational status (e.g. "1" for Operational).
            limit: Maximum orphan CIs to return (1-1000, default 25).
            scan_offset: Offset into the CI table to start scanning from. Use the
                        "next_scan_offset" value from a previous response to continue.

        Returns:
            JSON object with "ci_class", "count", "orphan_cis" list,
            "total_scanned" (CIs checked), and "next_scan_offset" (for continuation).
        """
        logger.info("find_orphan_cis: class=%s", ci_class)
        if err := _validate_cmdb_table(ci_class):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        limit = _clamp_limit(limit)
        scan_offset = _clamp_offset(scan_offset)

        try:
            # Fetch a batch of CIs to check
            query_parts: list[str] = []
            if operational_status:
                query_parts.append(f"operational_status={operational_status}")
            query = "^".join(query_parts)

            # Fetch more than limit since some will have relationships
            fetch_limit = min(limit * 4, _MAX_LIMIT)
            records = await client.get_records(
                table=ci_class,
                query=query,
                fields=_HEALTH_CI_FIELDS,
                limit=fetch_limit,
                offset=scan_offset,
            )

            # Batch check: find which CIs have any relationships using IN queries
            # instead of one aggregate call per CI (N+1 → ~2 calls per batch of 100)
            sys_ids = [r.get("sys_id", "") for r in records if r.get("sys_id")]
            has_relationships: set[str] = set()

            batch_size = 100
            for i in range(0, len(sys_ids), batch_size):
                batch = sys_ids[i:i + batch_size]
                batch_str = ",".join(batch)
                # Check as parent
                parent_rels = await client.get_records(
                    table="cmdb_rel_ci",
                    query=f"parentIN{batch_str}",
                    fields=["parent"],
                    limit=_MAX_LIMIT,
                )
                for rel in parent_rels:
                    has_relationships.add(resolve_ref(rel.get("parent", "")))
                # Check as child
                child_rels = await client.get_records(
                    table="cmdb_rel_ci",
                    query=f"childIN{batch_str}",
                    fields=["child"],
                    limit=_MAX_LIMIT,
                )
                for rel in child_rels:
                    has_relationships.add(resolve_ref(rel.get("child", "")))

            # Filter to orphans (CIs with no relationships)
            orphans: list[dict[str, Any]] = []
            scanned = 0
            for record in records:
                if len(orphans) >= limit:
                    break
                scanned += 1
                sys_id = record.get("sys_id", "")
                if sys_id and sys_id not in has_relationships:
                    orphans.append(record)

            # There may be more if we fetched a full batch OR if we stopped
            # early because the orphan limit was reached with unscanned records remaining
            has_unscanned = scanned < len(records)
            fetched_full_batch = len(records) == fetch_limit

            return _json({
                "ci_class": ci_class,
                "count": len(orphans),
                "orphan_cis": orphans,
                "total_scanned": scanned,
                "next_scan_offset": scan_offset + scanned,
                "may_have_more": has_unscanned or fetched_full_batch,
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
    async def find_duplicate_cis(
        ci_class: str = "cmdb_ci",
        match_field: str = "name",
        name_filter: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Find CIs that share the same value for a given field within a class.

        Identifies potential duplicates by grouping CIs on a field (typically name
        or serial_number) and returning groups with more than one record. Useful
        for finding CIs that may have been created by multiple discovery sources
        or manual entry.

        Args:
            ci_class: CMDB table to search (e.g. cmdb_ci_server). Defaults to cmdb_ci.
            match_field: Field to match duplicates on. Defaults to "name".
                        Common choices: "name", "serial_number", "asset_tag", "ip_address".
            name_filter: Optional STARTSWITH filter on the name field to narrow scope.
            limit: Maximum duplicate groups to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "ci_class", "match_field", "duplicate_group_count",
            and "duplicate_groups" (list of groups, each with the shared value and matching CIs).
        """
        logger.info("find_duplicate_cis: class=%s field=%s", ci_class, match_field)
        if err := _validate_cmdb_table(ci_class):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        # Validate match_field to prevent query injection
        allowed_fields = {"name", "serial_number", "asset_tag", "ip_address", "mac_address", "fqdn"}
        if match_field not in allowed_fields:
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Invalid match_field '{match_field}'.",
                "suggestion": f"Use one of: {', '.join(sorted(allowed_fields))}.",
                "retry": False,
            })

        try:
            # Use Aggregate API to find field values that appear more than once
            query_parts: list[str] = [f"{match_field}ISNOTEMPTY"]
            if name_filter:
                query_parts.append(f"nameSTARTSWITH{name_filter}")
            query = "^".join(query_parts)

            agg = await client.get_aggregate(
                table=ci_class,
                query=query,
                group_by=match_field,
            )

            # Parse aggregate results to find groups with count > 1
            agg_result = agg.get("result", [])
            if isinstance(agg_result, dict):
                agg_result = [agg_result] if agg_result else []

            duplicate_values: list[tuple[str, int]] = []
            for group in agg_result:
                stats = group.get("stats", {})
                count = int(stats.get("count", 0))
                if count > 1:
                    group_by_val = group.get("groupby_fields", [{}])
                    if isinstance(group_by_val, list) and group_by_val:
                        value = group_by_val[0].get("value", "")
                    else:
                        value = ""
                    if value:
                        duplicate_values.append((value, count))

            # Sort by count descending (worst duplicates first)
            duplicate_values.sort(key=lambda x: x[1], reverse=True)

            # Apply pagination
            paginated = duplicate_values[offset:offset + limit]

            # Fetch actual records for each duplicate group
            fields = ["sys_id", "name", "sys_class_name", match_field,
                       "operational_status", "discovery_source", "sys_updated_on"]
            duplicate_groups: list[dict[str, Any]] = []
            for value, count in paginated:
                # Skip values containing query operators to prevent injection
                if "^" in value:
                    continue
                group_records = await client.get_records(
                    table=ci_class,
                    query=f"{match_field}={value}",
                    fields=fields,
                    limit=10,  # Cap per-group to avoid excessive fetches
                )
                duplicate_groups.append({
                    "value": value,
                    "total_count": count,
                    "records": group_records,
                })

            return _json({
                "ci_class": ci_class,
                "match_field": match_field,
                "duplicate_group_count": len(duplicate_groups),
                "total_duplicate_groups": len(duplicate_values),
                "duplicate_groups": duplicate_groups,
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
    async def find_stale_cis(
        ci_class: str = "cmdb_ci",
        days: int = 90,
        operational_status: str = "1",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Find CIs that have not been updated within a specified number of days.

        Stale CIs are records whose sys_updated_on timestamp is older than the
        threshold. Filtering to operational CIs (status=1) by default highlights
        records that claim to be active but haven't been refreshed — often a sign
        of broken discovery or decommissioned assets.

        Args:
            ci_class: CMDB table to search (e.g. cmdb_ci_server). Defaults to cmdb_ci.
            days: Number of days since last update to consider stale (default 90).
                  Must be between 1 and 3650.
            operational_status: Filter by operational status. Defaults to "1" (Operational).
                              Set to empty string to include all statuses.
            limit: Maximum stale CIs to return (1-1000, default 25).
            offset: Pagination offset.

        Returns:
            JSON object with "ci_class", "stale_days", "count", and "stale_cis" list
            ordered by sys_updated_on ascending (most stale first).
        """
        logger.info("find_stale_cis: class=%s days=%d", ci_class, days)
        if err := _validate_cmdb_table(ci_class):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        days = max(1, min(days, 3650))

        try:
            query_parts = [f"sys_updated_on<javascript:gs.daysAgo({days})"]
            if operational_status:
                query_parts.append(f"operational_status={operational_status}")
            query = "^".join(query_parts)

            records = await client.get_records(
                table=ci_class,
                query=query,
                fields=_HEALTH_CI_FIELDS,
                limit=limit,
                offset=offset,
                order_by="ORDERBYsys_updated_on",
            )

            # Also get a count for context
            agg = await client.get_aggregate(
                table=ci_class,
                query=query,
            )
            total_stale = _extract_count(agg)

            return _json({
                "ci_class": ci_class,
                "stale_days": days,
                "count": len(records),
                "total_stale": total_stale,
                "stale_cis": records,
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
    async def cmdb_health_summary(
        ci_class: str = "cmdb_ci",
        stale_days: int = 90,
    ) -> str:
        """Produce an overall CMDB health summary with key metrics.

        Aggregates several health indicators into a single overview:
        - Total CI count and breakdown by operational status
        - Count of stale CIs (not updated in N days)
        - Count of CIs missing key fields (name, sys_class_name)
        - Breakdown by discovery source

        This is an efficient summary using only Aggregate API calls — no record
        fetches. Use the specific tools (find_orphan_cis, find_duplicate_cis,
        find_stale_cis) for detailed record-level results.

        Args:
            ci_class: CMDB table to summarize (e.g. cmdb_ci_server). Defaults to cmdb_ci.
            stale_days: Number of days threshold for staleness (default 90).

        Returns:
            JSON object with "ci_class", "total_count", "by_operational_status",
            "stale_count", "missing_name_count", and "by_discovery_source".
        """
        logger.info("cmdb_health_summary: class=%s stale_days=%d", ci_class, stale_days)
        if err := _validate_cmdb_table(ci_class):
            return _json({
                "error": True, "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })
        stale_days = max(1, min(stale_days, 3650))

        try:
            stale_query = f"sys_updated_on<javascript:gs.daysAgo({stale_days})"

            async def _safe_agg(**kwargs: str) -> dict:
                try:
                    return await client.get_aggregate(table=ci_class, **kwargs)
                except ServiceNowError:
                    return {}

            # Run all 5 independent aggregate calls in parallel
            total_agg, status_agg, stale_agg, missing_name_agg, source_agg = (
                await asyncio.gather(
                    _safe_agg(),
                    _safe_agg(group_by="operational_status"),
                    _safe_agg(query=stale_query),
                    _safe_agg(query="nameISEMPTY"),
                    _safe_agg(group_by="discovery_source"),
                )
            )

            return _json({
                "ci_class": ci_class,
                "total_count": _extract_count(total_agg),
                "by_operational_status": _parse_agg_groups(status_agg, empty_label="unknown"),
                "stale_count": _extract_count(stale_agg),
                "stale_days": stale_days,
                "missing_name_count": _extract_count(missing_name_agg),
                "by_discovery_source": _parse_agg_groups(source_agg, empty_label="(empty)"),
            })
        except ServiceNowError as e:
            return e.to_json()
