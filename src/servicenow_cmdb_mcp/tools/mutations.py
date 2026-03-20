"""CI mutation tools — two-phase preview/confirm for updates and creates."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import NotFoundError, ServiceNowError
from servicenow_cmdb_mcp.tools._utils import _TABLE_NAME_RE, _json, _validate_cmdb_table

logger = logging.getLogger(__name__)

# Token TTL in seconds — tokens expire after 5 minutes
_TOKEN_TTL = 300

# Fields that must never be set via mutation tools
_BLOCKED_FIELDS = frozenset({
    "sys_id",
    "sys_created_on",
    "sys_created_by",
    "sys_updated_on",
    "sys_updated_by",
    "sys_mod_count",
    "sys_tags",
})

# Max pending operations to prevent unbounded memory growth
_MAX_PENDING = 50

# Max length for a single field value
_MAX_FIELD_VALUE_LENGTH = 10_000


def _generate_token() -> str:
    """Generate a short random confirmation token."""
    return secrets.token_hex(8)


def _validate_fields(fields: dict[str, str]) -> str | None:
    """Validate field dict. Returns error message or None if valid."""
    if not fields:
        return "No fields provided."
    blocked = set(fields.keys()) & _BLOCKED_FIELDS
    if blocked:
        return f"Cannot set system-managed fields: {', '.join(sorted(blocked))}."
    for key, value in fields.items():
        if not _TABLE_NAME_RE.match(key):
            return f"Invalid field name: '{key}'. Must contain only ASCII letters, digits, and underscores."
        if isinstance(value, str) and len(value) > _MAX_FIELD_VALUE_LENGTH:
            return f"Field '{key}' value exceeds maximum length of {_MAX_FIELD_VALUE_LENGTH} characters."
    return None


class _PendingOperation:
    """Stores the details of a previewed mutation awaiting confirmation."""

    __slots__ = ("token", "operation", "table", "sys_id", "fields", "created_at")

    def __init__(
        self,
        token: str,
        operation: str,
        table: str,
        fields: dict[str, str],
        sys_id: str = "",
    ) -> None:
        self.token = token
        self.operation = operation
        self.table = table
        self.sys_id = sys_id
        self.fields = fields
        self.created_at = time.monotonic()

    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > _TOKEN_TTL


def register_mutation_tools(mcp: FastMCP, client: ServiceNowClient) -> None:
    """Register all CI mutation tools on the MCP server."""

    # In-memory store for pending operations, keyed by token
    pending: dict[str, _PendingOperation] = {}

    def _cleanup_expired() -> None:
        """Remove expired tokens and enforce max pending cap."""
        expired = [t for t, op in pending.items() if op.is_expired()]
        for t in expired:
            del pending[t]
        # If still over cap, remove oldest entries
        if len(pending) > _MAX_PENDING:
            by_age = sorted(pending.items(), key=lambda x: x[1].created_at)
            for t, _ in by_age[: len(pending) - _MAX_PENDING]:
                del pending[t]

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def preview_ci_update(
        sys_id: str,
        table: str,
        fields: dict[str, str],
    ) -> str:
        """Preview changes to an existing CI before applying them.

        Fetches the current record, computes a diff against the proposed changes,
        and returns a confirmation token. No changes are made to ServiceNow.

        The confirmation token is valid for 5 minutes. Pass it to confirm_ci_update
        to execute the change.

        Prerequisites: Use search_cis or get_ci_details to find the CI sys_id first.

        Typical workflow: preview_ci_update → review diff → confirm_ci_update

        Args:
            sys_id: The sys_id of the CI to update.
            table: The CMDB table (e.g. cmdb_ci_server). Must be the specific class table.
            fields: Dictionary of field names to new values. Example:
                    {"operational_status": "2", "install_status": "7"}.
                    System fields (sys_id, sys_created_on, etc.) cannot be set.

        Returns:
            JSON object with "token" (confirmation token), "sys_id", "table",
            "diff" (list of field changes with old/new values), and "fields" (proposed values).
        """
        logger.info("preview_ci_update: sys_id=%s table=%s", sys_id, table)
        _cleanup_expired()

        if not sys_id or not sys_id.strip():
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "sys_id must not be empty.",
                "suggestion": "Provide the sys_id of the CI to update.",
                "retry": False,
            })

        if err := _validate_cmdb_table(table):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })

        if err := _validate_fields(fields):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Check field names and values.",
                "retry": False,
            })

        try:
            # Fetch current record to build diff
            current = await client.get_record(
                table=table,
                sys_id=sys_id,
                fields=list(fields.keys()) + ["sys_id", "name", "sys_class_name"],
            )
            if not current:
                return _json({
                    "error": True,
                    "category": "NotFoundError",
                    "message": f"No CI found with sys_id '{sys_id}' in table '{table}'.",
                    "suggestion": "Verify the sys_id and table name.",
                    "retry": False,
                })

            # Build diff
            diff: list[dict[str, Any]] = []
            for field, new_value in fields.items():
                old_value = str(current.get(field, ""))
                diff.append({
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed": old_value != new_value,
                })

            # Generate token and store pending operation
            token = _generate_token()
            pending[token] = _PendingOperation(
                token=token,
                operation="update",
                table=table,
                sys_id=sys_id,
                fields=fields,
            )

            return _json({
                "token": token,
                "operation": "update",
                "sys_id": sys_id,
                "table": table,
                "ci_name": current.get("name", ""),
                "ci_class": current.get("sys_class_name", ""),
                "diff": diff,
                "fields": fields,
                "expires_in_seconds": _TOKEN_TTL,
                "message": f"Review the diff above. To apply, call confirm_ci_update with token '{token}'.",
            })
        except NotFoundError:
            return _json({
                "error": True,
                "category": "NotFoundError",
                "message": f"No CI found with sys_id '{sys_id}' in table '{table}'.",
                "suggestion": "Verify the sys_id and table name.",
                "retry": False,
            })
        except ServiceNowError as e:
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        },
    )
    async def confirm_ci_update(
        token: str,
    ) -> str:
        """Execute a previously previewed CI update.

        Prerequisites: You MUST call preview_ci_update first and use the token from its response.

        Requires the confirmation token returned by preview_ci_update. The token
        is single-use and expires after 5 minutes.

        Args:
            token: The confirmation token from preview_ci_update.

        Returns:
            JSON object with "success", "sys_id", "table", and the "updated_record".
        """
        logger.info("confirm_ci_update: token=%s", token[:4] + "****" if token else "(empty)")
        _cleanup_expired()

        if not token or not token.strip():
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token must not be empty.",
                "suggestion": "Call preview_ci_update first to get a token.",
                "retry": False,
            })

        op = pending.get(token)
        if op is None:
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Invalid or expired confirmation token.",
                "suggestion": "Call preview_ci_update again to get a new token.",
                "retry": False,
            })

        if op.is_expired():
            del pending[token]
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token has expired.",
                "suggestion": "Call preview_ci_update again to get a new token.",
                "retry": False,
            })

        # Consume token — single-use
        del pending[token]

        if op.operation != "update":
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Token is for a '{op.operation}' operation, not 'update'.",
                "suggestion": "Use confirm_ci_create for create tokens.",
                "retry": False,
            })

        try:
            response = await client.patch(
                path=f"/api/now/table/{op.table}/{op.sys_id}",
                json_body=op.fields,
                params={"sysparm_fields": ",".join(
                    list(op.fields.keys()) + ["sys_id", "name", "sys_class_name", "sys_updated_on"],
                )},
            )
            updated = response.get("result", response)

            return _json({
                "success": True,
                "operation": "update",
                "sys_id": op.sys_id,
                "table": op.table,
                "updated_record": updated,
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
    async def preview_ci_create(
        table: str,
        fields: dict[str, str],
    ) -> str:
        """Preview the creation of a new CI before inserting it.

        Validates the fields and returns a confirmation token. No record is
        created in ServiceNow until confirm_ci_create is called.

        The confirmation token is valid for 5 minutes.

        Args:
            table: The CMDB table to create the CI in (e.g. cmdb_ci_server).
            fields: Dictionary of field names to values. Must include at minimum
                    a "name" field. Example:
                    {"name": "web-server-01", "ip_address": "10.0.1.5",
                     "operational_status": "1"}.
                    System fields (sys_id, sys_created_on, etc.) cannot be set.

        Returns:
            JSON object with "token" (confirmation token), "table",
            "fields" (the values to be created), and a human-readable message.
        """
        logger.info("preview_ci_create: table=%s", table)
        _cleanup_expired()

        if err := _validate_cmdb_table(table):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid CMDB table name (e.g. cmdb_ci_server).",
                "retry": False,
            })

        if err := _validate_fields(fields):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Check field names and values.",
                "retry": False,
            })

        if "name" not in fields:
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Field 'name' is required for CI creation.",
                "suggestion": "Include a 'name' field in the fields dict.",
                "retry": False,
            })

        # Generate token and store pending operation
        token = _generate_token()
        pending[token] = _PendingOperation(
            token=token,
            operation="create",
            table=table,
            fields=fields,
        )

        return _json({
            "token": token,
            "operation": "create",
            "table": table,
            "fields": fields,
            "expires_in_seconds": _TOKEN_TTL,
            "message": f"Review the fields above. To create, call confirm_ci_create with token '{token}'.",
        })

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        },
    )
    async def confirm_ci_create(
        token: str,
    ) -> str:
        """Execute a previously previewed CI creation.

        Prerequisites: You MUST call preview_ci_create first and use the token from its response.

        Requires the confirmation token returned by preview_ci_create. The token
        is single-use and expires after 5 minutes.

        Args:
            token: The confirmation token from preview_ci_create.

        Returns:
            JSON object with "success", "sys_id" (of the new CI), "table",
            and the "created_record".
        """
        logger.info("confirm_ci_create: token=%s", token[:4] + "****" if token else "(empty)")
        _cleanup_expired()

        if not token or not token.strip():
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token must not be empty.",
                "suggestion": "Call preview_ci_create first to get a token.",
                "retry": False,
            })

        op = pending.get(token)
        if op is None:
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Invalid or expired confirmation token.",
                "suggestion": "Call preview_ci_create again to get a new token.",
                "retry": False,
            })

        if op.is_expired():
            del pending[token]
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token has expired.",
                "suggestion": "Call preview_ci_create again to get a new token.",
                "retry": False,
            })

        # Consume token — single-use
        del pending[token]

        if op.operation != "create":
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Token is for a '{op.operation}' operation, not 'create'.",
                "suggestion": "Use confirm_ci_update for update tokens.",
                "retry": False,
            })

        try:
            response = await client.post(
                path=f"/api/now/table/{op.table}",
                json_body=op.fields,
                params={"sysparm_fields": ",".join(
                    list(op.fields.keys()) + ["sys_id", "name", "sys_class_name", "sys_created_on"],
                )},
            )
            created = response.get("result", response)

            return _json({
                "success": True,
                "operation": "create",
                "sys_id": created.get("sys_id", ""),
                "table": op.table,
                "created_record": created,
            })
        except ServiceNowError as e:
            return e.to_json()
