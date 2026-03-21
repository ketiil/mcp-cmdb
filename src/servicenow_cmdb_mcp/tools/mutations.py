"""CI mutation tools — two-phase preview/confirm for updates and creates."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.errors import NotFoundError, ServiceNowError
from servicenow_cmdb_mcp.redaction import redact_credentials
from servicenow_cmdb_mcp.tools._utils import _TABLE_NAME_RE, _json, _require_client, _validate_cmdb_table, _validate_sys_id

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


def _redact_field_values(fields: dict[str, str]) -> dict[str, Any]:
    """Apply credential redaction to field values for safe display in previews."""
    return {k: redact_credentials(v) if isinstance(v, str) else v for k, v in fields.items()}


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

    # Completed operations cache for idempotent retries — token → (expiry_timestamp, json_result)
    _completed_ops: dict[str, tuple[float, str]] = {}

    # TTL for completed operation cache (seconds)
    _COMPLETED_TTL = 60

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
        # Clean up expired completed operations and enforce cap
        now = time.time()
        expired_completed = [t for t, (exp, _) in _completed_ops.items() if now >= exp]
        for t in expired_completed:
            del _completed_ops[t]
        if len(_completed_ops) > _MAX_PENDING:
            # Evict oldest entries (earliest expiry) to stay within bounds
            by_expiry = sorted(_completed_ops.items(), key=lambda x: x[1][0])
            for t, _ in by_expiry[: len(_completed_ops) - _MAX_PENDING]:
                del _completed_ops[t]

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            # Idempotent w.r.t. ServiceNow — generates a new token internally
            # but has no external side effects.
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
        if err := _require_client(client):
            return err
        _cleanup_expired()

        if err := _validate_sys_id(sys_id):
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": err,
                "suggestion": "Provide a valid 32-character hex sys_id of the CI to update.",
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
                "fields": _redact_field_values(fields),
                "expires_in_seconds": _TOKEN_TTL,
                "message": f"Review the diff above. To apply, call confirm_ci_update with token '{token}'.",
                "note": "Write permission is not verified until confirm. The confirm step may fail with PermissionError if the service account lacks write access to this table.",
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
            # Idempotent within 60s via _completed_ops cache — safe to retry
            "idempotentHint": True,
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
        if err := _require_client(client):
            return err
        _cleanup_expired()

        if not token or not token.strip():
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token must not be empty.",
                "suggestion": "Call preview_ci_update first to get a token.",
                "retry": False,
            })

        # Check completed operations cache for idempotent retry
        if token in _completed_ops:
            expiry, cached_result = _completed_ops[token]
            if time.time() < expiry:
                logger.info("confirm_ci_update: returning cached result for token=%s", token[:4] + "****")
                return cached_result
            else:
                del _completed_ops[token]

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

        if op.operation != "update":
            # Don't consume — the token belongs to a different confirm handler.
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Token is for a '{op.operation}' operation, not 'update'.",
                "suggestion": "Use confirm_ci_create for create tokens.",
                "retry": False,
            })

        logger.info(
            "AUDIT confirm_ci_update: user=%s table=%s sys_id=%s fields=%s",
            client.username if client else "unknown", op.table, op.sys_id, list(op.fields.keys()),
        )

        try:
            response = await client.patch(
                path=f"/api/now/table/{op.table}/{op.sys_id}",
                json_body=op.fields,
                params={"sysparm_fields": ",".join(
                    list(op.fields.keys()) + ["sys_id", "name", "sys_class_name", "sys_updated_on"],
                )},
            )
            updated = response.get("result", response)

            # Consume token only after successful write
            del pending[token]

            logger.info(
                "AUDIT confirm_ci_update: SUCCESS user=%s table=%s sys_id=%s",
                client.username if client else "unknown", op.table, op.sys_id,
            )

            result = _json({
                "success": True,
                "operation": "update",
                "sys_id": op.sys_id,
                "table": op.table,
                "updated_record": updated,
                "suggested_next": f"Use get_ci_details(sys_id='{op.sys_id}', table='{op.table}') to verify the update, or get_ci_relationships(ci_sys_id='{op.sys_id}') to check downstream impact.",
            })

            # Cache result for idempotent retries
            _completed_ops[token] = (time.time() + _COMPLETED_TTL, result)

            return result
        except ServiceNowError as e:
            logger.error(
                "AUDIT confirm_ci_update: FAILED user=%s table=%s sys_id=%s error=%s",
                client.username if client else "unknown", op.table, op.sys_id, e.category,
            )
            # Preserve token for retryable errors (429, 5xx, timeout) so the
            # agent can retry without re-previewing.  Consume on permanent errors.
            if not e.retry:
                pending.pop(token, None)
            return e.to_json()

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            # Idempotent w.r.t. ServiceNow — generates a new token internally
            # but has no external side effects.
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

        Prerequisites: Call describe_ci_class(table) first to review mandatory
        fields and valid values before composing the fields dict. Call
        suggest_table if you are unsure of the correct table name.

        Typical workflow: suggest_table → describe_ci_class → preview_ci_create → confirm_ci_create

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
        if err := _require_client(client):
            return err
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
            "fields": _redact_field_values(fields),
            "expires_in_seconds": _TOKEN_TTL,
            "message": f"Review the fields above. To create, call confirm_ci_create with token '{token}'.",
            "note": "Write permission is not verified until confirm. The confirm step may fail with PermissionError if the service account lacks write access to this table.",
        })

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            # Idempotent within 60s via _completed_ops cache — safe to retry
            "idempotentHint": True,
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
        if err := _require_client(client):
            return err
        _cleanup_expired()

        if not token or not token.strip():
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": "Confirmation token must not be empty.",
                "suggestion": "Call preview_ci_create first to get a token.",
                "retry": False,
            })

        # Check completed operations cache for idempotent retry
        if token in _completed_ops:
            expiry, cached_result = _completed_ops[token]
            if time.time() < expiry:
                logger.info("confirm_ci_create: returning cached result for token=%s", token[:4] + "****")
                return cached_result
            else:
                del _completed_ops[token]

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

        if op.operation != "create":
            # Don't consume — the token belongs to a different confirm handler.
            return _json({
                "error": True,
                "category": "ValidationError",
                "message": f"Token is for a '{op.operation}' operation, not 'create'.",
                "suggestion": "Use confirm_ci_update for update tokens.",
                "retry": False,
            })

        logger.info(
            "AUDIT confirm_ci_create: user=%s table=%s fields=%s",
            client.username if client else "unknown", op.table, list(op.fields.keys()),
        )

        try:
            response = await client.post(
                path=f"/api/now/table/{op.table}",
                json_body=op.fields,
                params={"sysparm_fields": ",".join(
                    list(op.fields.keys()) + ["sys_id", "name", "sys_class_name", "sys_created_on"],
                )},
            )
            created = response.get("result", response)

            # Consume token only after successful write
            del pending[token]

            logger.info(
                "AUDIT confirm_ci_create: SUCCESS user=%s table=%s sys_id=%s",
                client.username if client else "unknown", op.table, created.get("sys_id", ""),
            )

            new_sys_id = created.get("sys_id", "")
            result = _json({
                "success": True,
                "operation": "create",
                "sys_id": new_sys_id,
                "table": op.table,
                "created_record": created,
                "suggested_next": f"Use get_ci_details(sys_id='{new_sys_id}', table='{op.table}') to verify the new record, or describe_ci_class(class_name='{op.table}') to review available fields.",
            })

            # Cache result for idempotent retries
            _completed_ops[token] = (time.time() + _COMPLETED_TTL, result)

            return result
        except ServiceNowError as e:
            logger.error(
                "AUDIT confirm_ci_create: FAILED user=%s table=%s error=%s",
                client.username if client else "unknown", op.table, e.category,
            )
            # Preserve token for retryable errors (429, 5xx, timeout) so the
            # agent can retry without re-previewing.  Consume on permanent errors.
            if not e.retry:
                pending.pop(token, None)
            return e.to_json()
