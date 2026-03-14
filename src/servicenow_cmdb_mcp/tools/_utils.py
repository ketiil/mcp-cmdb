"""Shared utilities for tool modules — validation, clamping, serialization."""

from __future__ import annotations

import json
import re
from typing import Any

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


def _validate_sys_id(sys_id: str) -> str | None:
    """Return a validation error message if sys_id is invalid, else None.

    sys_ids are 32-char hex strings; reject anything with non-alphanumeric chars.
    """
    if not sys_id or not sys_id.strip():
        return "sys_id must not be empty."
    if not all(c.isalnum() for c in sys_id):
        return f"Invalid sys_id format: '{sys_id}'. Must contain only alphanumeric characters."
    return None
