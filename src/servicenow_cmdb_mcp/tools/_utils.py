"""Shared utilities for tool modules — validation, clamping, serialization."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from servicenow_cmdb_mcp.client import ServiceNowClient

from servicenow_cmdb_mcp.errors import ServiceNowError

logger = logging.getLogger(__name__)

_MAX_LIMIT = 1000

# Regex for valid ServiceNow table/field names: ASCII letters, digits, underscores only.
# Rejects Unicode homoglyphs that str.isidentifier() would accept.
_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _json(result: Any) -> str:
    """Serialize a result to JSON for tool responses."""
    return json.dumps(result, indent=2, default=str)


def _clamp_limit(limit: int) -> int:
    """Clamp limit to valid range [1, 1000]."""
    return max(1, min(limit, _MAX_LIMIT))


def _clamp_offset(offset: int) -> int:
    """Clamp offset to non-negative."""
    return max(0, offset)


def _validate_table_name(table: str) -> str | None:
    """Validate table name is safe for URL interpolation. Returns error or None.

    Uses an explicit ASCII regex instead of str.isidentifier() to reject
    Unicode homoglyphs (e.g. Cyrillic characters that look like Latin).
    """
    if not table or not table.strip():
        return "table must not be empty."
    if not _TABLE_NAME_RE.match(table):
        return f"Invalid table name: '{table}'. Must contain only ASCII letters, digits, and underscores."
    return None


# Prefixes allowed for tables the LLM can directly query or mutate.
# Prevents querying sensitive tables like sys_user or sys_user_has_role.
_CMDB_TABLE_PREFIXES = ("cmdb_",)


def _validate_cmdb_table(table: str) -> str | None:
    """Validate table name format AND that it is a CMDB table.

    Returns an error string if invalid, None if valid.
    Combines format validation with an allowlist check to prevent
    the LLM from querying sensitive non-CMDB tables.
    """
    if err := _validate_table_name(table):
        return err
    if not table.startswith(_CMDB_TABLE_PREFIXES):
        return (
            f"Table '{table}' is not a CMDB table. "
            "Only tables starting with 'cmdb_' are allowed. "
            "Use suggest_table or list_ci_classes to find the right table."
        )
    return None


def _extract_agg_count(agg_result: dict[str, Any]) -> int:
    """Extract integer count from an Aggregate API response."""
    result = agg_result.get("result", agg_result)
    if isinstance(result, dict):
        try:
            return int(result.get("stats", {}).get("count", 0))
        except (ValueError, TypeError):
            return 0
    return 0


async def _safe_total(client: ServiceNowClient, table: str, query: str) -> int | None:
    """Get total record count via aggregate, returning None on failure."""
    try:
        agg = await client.get_aggregate(table=table, query=query)
        return _extract_agg_count(agg)
    except ServiceNowError:
        logger.debug("Aggregate count failed for %s", table)
        return None


def _has_more(total: int | None, offset: int, page_len: int, limit: int) -> bool:
    """Determine if more results exist beyond the current page.

    When total is known, uses exact comparison. When total is unknown
    (aggregate call failed), falls back to a heuristic: if a full page
    was returned, there are likely more results.
    """
    if total is not None:
        return offset + page_len < total
    return page_len == limit


def _nav_url(base_url: str, table: str, sys_id: str) -> str:
    """Build a ServiceNow navigation URL for a record."""
    return f"{base_url}/nav_to.do?uri={table}.do%3Fsys_id%3D{sys_id}"


def _require_client(client: ServiceNowClient | None) -> str | None:
    """Return an AuthError JSON response if client is None, else None.

    Use at the top of every tool handler to guard against missing credentials:
        if (err := _require_client(client)):
            return err
    """
    if client is None:
        return _json({
            "error": True,
            "category": "AuthError",
            "message": "ServiceNow credentials are not configured.",
            "suggestion": "Set environment variables: SN_INSTANCE_URL, SN_CLIENT_ID, SN_CLIENT_SECRET, SN_USERNAME, SN_PASSWORD.",
            "retry": False,
        })
    return None


def _validate_sys_id(sys_id: str) -> str | None:
    """Return a validation error message if sys_id is invalid, else None.

    sys_ids are 32-char hex strings; reject anything with non-alphanumeric chars.
    """
    if not sys_id or not sys_id.strip():
        return "sys_id must not be empty."
    if not all(c.isalnum() for c in sys_id):
        return f"Invalid sys_id format: '{sys_id}'. Must contain only alphanumeric characters."
    return None
