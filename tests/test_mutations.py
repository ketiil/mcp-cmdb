"""Tests for tools/mutations.py — two-phase preview/confirm CI mutations."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from servicenow_cmdb_mcp.errors import NotFoundError, RateLimitError, SNPermissionError
from servicenow_cmdb_mcp.tools._utils import _validate_table_name
from servicenow_cmdb_mcp.tools.mutations import (
    _BLOCKED_FIELDS,
    _MAX_FIELD_VALUE_LENGTH,
    _validate_fields,
    register_mutation_tools,
)

# ── Fake sys_ids ────────────────────────────────────────────────────

CI_A = "a" * 32


# ── Helpers ─────────────────────────────────────────────────────────

def _ci_record(sys_id: str = CI_A, name: str = "Server-A", cls: str = "cmdb_ci_server"):
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": cls,
        "operational_status": "1",
        "ip_address": "10.0.1.1",
    }


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_record = AsyncMock(return_value=_ci_record())
    client.patch = AsyncMock(return_value={"result": {**_ci_record(), "operational_status": "2"}})
    client.post = AsyncMock(return_value={"result": {**_ci_record(), "sys_id": "new123"}})
    return client


@pytest.fixture
def tools(mock_client):
    """Register mutation tools and return the tool functions."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register_mutation_tools(mcp, mock_client)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn
    return tool_map


# ── Unit tests: _validate_fields ────────────────────────────────────

class TestValidateFields:
    def test_valid_fields(self):
        assert _validate_fields({"name": "test", "ip_address": "10.0.0.1"}) is None

    def test_empty_fields(self):
        assert _validate_fields({}) is not None

    def test_blocked_sys_id(self):
        err = _validate_fields({"sys_id": "abc123"})
        assert err is not None
        assert "sys_id" in err

    def test_blocked_sys_created_on(self):
        err = _validate_fields({"sys_created_on": "2025-01-01"})
        assert err is not None

    def test_all_blocked_fields_rejected(self):
        for field in _BLOCKED_FIELDS:
            err = _validate_fields({field: "value"})
            assert err is not None, f"Field '{field}' should be blocked"

    def test_invalid_field_name(self):
        err = _validate_fields({"bad-field": "value"})
        assert err is not None
        assert "Invalid field name" in err

    def test_value_too_long(self):
        err = _validate_fields({"name": "x" * (_MAX_FIELD_VALUE_LENGTH + 1)})
        assert err is not None
        assert "maximum length" in err

    def test_value_at_max_length_ok(self):
        assert _validate_fields({"name": "x" * _MAX_FIELD_VALUE_LENGTH}) is None


class TestValidateTableName:
    def test_valid_table(self):
        assert _validate_table_name("cmdb_ci_server") is None

    def test_empty(self):
        assert _validate_table_name("") is not None

    def test_blank(self):
        assert _validate_table_name("   ") is not None

    def test_path_traversal(self):
        err = _validate_table_name("cmdb_ci/../sys_user")
        assert err is not None
        assert "Invalid table name" in err

    def test_slash(self):
        err = _validate_table_name("cmdb_ci/foo")
        assert err is not None

    def test_special_chars(self):
        err = _validate_table_name("cmdb_ci; DROP TABLE")
        assert err is not None


# ── preview_ci_update ───────────────────────────────────────────────

class TestPreviewCiUpdate:
    @pytest.mark.asyncio
    async def test_returns_diff_and_token(self, mock_client, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A,
            table="cmdb_ci_server",
            fields={"operational_status": "2"},
        ))
        assert "token" in result
        assert result["operation"] == "update"
        assert result["sys_id"] == CI_A
        assert result["table"] == "cmdb_ci_server"
        assert len(result["diff"]) == 1
        assert result["diff"][0]["field"] == "operational_status"
        assert result["diff"][0]["old_value"] == "1"
        assert result["diff"][0]["new_value"] == "2"
        assert result["diff"][0]["changed"] is True

    @pytest.mark.asyncio
    async def test_preview_includes_permission_note(self, mock_client, tools):
        """Preview response should include a note about write permission."""
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A,
            table="cmdb_ci_server",
            fields={"operational_status": "2"},
        ))
        assert "note" in result
        assert "Write permission is not verified until confirm" in result["note"]
        assert "PermissionError" in result["note"]

    @pytest.mark.asyncio
    async def test_diff_shows_unchanged(self, mock_client, tools):
        """Fields that are the same should show changed=False."""
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A,
            table="cmdb_ci_server",
            fields={"operational_status": "1"},  # Same as current
        ))
        assert result["diff"][0]["changed"] is False

    @pytest.mark.asyncio
    async def test_empty_sys_id(self, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id="", table="cmdb_ci", fields={"name": "x"},
        ))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_empty_table(self, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="", fields={"name": "x"},
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_path_traversal_table(self, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci/../sys_user", fields={"name": "x"},
        ))
        assert result["error"] is True
        assert result["category"] == "ValidationError"

    @pytest.mark.asyncio
    async def test_blocked_fields_rejected(self, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"sys_id": "hack"},
        ))
        assert result["error"] is True
        assert "system-managed" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_fields_rejected(self, tools):
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={},
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_ci_not_found(self, mock_client, tools):
        mock_client.get_record.return_value = None
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"name": "x"},
        ))
        assert result["error"] is True
        assert result["category"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_ci_not_found_exception(self, mock_client, tools):
        mock_client.get_record.side_effect = NotFoundError("Not found")
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"name": "x"},
        ))
        assert result["error"] is True
        assert result["category"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        mock_client.get_record.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"name": "x"},
        ))
        assert result["error"] is True
        assert result["category"] == "PermissionError"


# ── confirm_ci_update ───────────────────────────────────────────────

class TestConfirmCiUpdate:
    @pytest.mark.asyncio
    async def test_full_update_flow(self, mock_client, tools):
        """Preview then confirm should execute the PATCH."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci_server",
            fields={"operational_status": "2"},
        ))
        token = preview["token"]

        result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["success"] is True
        assert result["operation"] == "update"
        assert result["sys_id"] == CI_A

        # Verify PATCH was called correctly
        mock_client.patch.assert_called_once()
        call_args = mock_client.patch.call_args
        assert f"/api/now/table/cmdb_ci_server/{CI_A}" in call_args.kwargs["path"]
        assert call_args.kwargs["json_body"] == {"operational_status": "2"}

    @pytest.mark.asyncio
    async def test_empty_token(self, tools):
        result = _parse(await tools["confirm_ci_update"](token=""))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_invalid_token(self, tools):
        result = _parse(await tools["confirm_ci_update"](token="nonexistent"))
        assert result["error"] is True
        assert "Invalid or expired" in result["message"]

    @pytest.mark.asyncio
    async def test_token_retry_within_grace_period(self, mock_client, tools):
        """Retry within 60s grace period should return cached result."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "new-name"},
        ))
        token = preview["token"]

        # First use succeeds
        result1 = _parse(await tools["confirm_ci_update"](token=token))
        assert result1["success"] is True

        # Second use within grace period returns same cached result
        result2 = _parse(await tools["confirm_ci_update"](token=token))
        assert result2["success"] is True
        assert result2 == result1

        # Should NOT have called patch a second time
        assert mock_client.patch.call_count == 1

    @pytest.mark.asyncio
    async def test_token_retry_after_grace_period_expires(self, mock_client, tools):
        """Retry after the 60s grace period should fail."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "new-name"},
        ))
        token = preview["token"]

        result1 = _parse(await tools["confirm_ci_update"](token=token))
        assert result1["success"] is True

        # Simulate grace period expiry
        with patch("servicenow_cmdb_mcp.tools.mutations.time.time", return_value=time.time() + 120):
            result2 = _parse(await tools["confirm_ci_update"](token=token))
        assert result2["error"] is True
        assert "Invalid or expired" in result2["message"]

    @pytest.mark.asyncio
    async def test_expired_token(self, mock_client, tools):
        """Expired tokens should be rejected."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "x"},
        ))
        token = preview["token"]

        # Simulate expiration by patching time.monotonic
        with patch("servicenow_cmdb_mcp.tools.mutations.time.monotonic", return_value=time.monotonic() + 600):
            result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["error"] is True
        assert "expired" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_wrong_operation_type(self, mock_client, tools):
        """A create token should not work with confirm_ci_update."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "new-ci"},
        ))
        token = preview["token"]

        result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["error"] is True
        assert "create" in result["message"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "x"},
        ))
        mock_client.patch.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["confirm_ci_update"](token=preview["token"]))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_token_preserved_on_retryable_error(self, mock_client, tools):
        """Retryable errors (429, 5xx) should preserve token for retry."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "new-name"},
        ))
        token = preview["token"]

        # First attempt: retryable error
        mock_client.patch.side_effect = RateLimitError("Rate limited", retry_after=5)
        result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["error"] is True
        assert result["retry"] is True

        # Second attempt: succeeds — token was preserved
        mock_client.patch.side_effect = None
        mock_client.patch.return_value = {"result": {**_ci_record(), "name": "new-name"}}
        result2 = _parse(await tools["confirm_ci_update"](token=token))
        assert result2["success"] is True

    @pytest.mark.asyncio
    async def test_token_consumed_on_permanent_error(self, mock_client, tools):
        """Permanent errors (403) should consume token."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci",
            fields={"name": "new-name"},
        ))
        token = preview["token"]

        mock_client.patch.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["error"] is True

        # Token is now consumed — retry should fail
        mock_client.patch.side_effect = None
        result2 = _parse(await tools["confirm_ci_update"](token=token))
        assert result2["error"] is True
        assert "Invalid or expired" in result2["message"]

    @pytest.mark.asyncio
    async def test_wrong_operation_type_preserves_token(self, mock_client, tools):
        """Operation-type mismatch should NOT consume the token."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "new-ci"},
        ))
        token = preview["token"]

        # Try with wrong handler — should error but preserve token
        result = _parse(await tools["confirm_ci_update"](token=token))
        assert result["error"] is True
        assert "create" in result["message"]

        # Token should still work with correct handler
        result2 = _parse(await tools["confirm_ci_create"](token=token))
        assert result2["success"] is True


# ── preview_ci_create ───────────────────────────────────────────────

class TestPreviewCiCreate:
    @pytest.mark.asyncio
    async def test_returns_token_and_fields(self, tools):
        result = _parse(await tools["preview_ci_create"](
            table="cmdb_ci_server",
            fields={"name": "new-server", "ip_address": "10.0.1.5"},
        ))
        assert "token" in result
        assert result["operation"] == "create"
        assert result["table"] == "cmdb_ci_server"
        assert result["fields"]["name"] == "new-server"

    @pytest.mark.asyncio
    async def test_preview_includes_permission_note(self, tools):
        """Preview response should include a note about write permission."""
        result = _parse(await tools["preview_ci_create"](
            table="cmdb_ci_server",
            fields={"name": "new-server"},
        ))
        assert "note" in result
        assert "Write permission is not verified until confirm" in result["note"]
        assert "PermissionError" in result["note"]

    @pytest.mark.asyncio
    async def test_empty_table(self, tools):
        result = _parse(await tools["preview_ci_create"](
            table="", fields={"name": "x"},
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_missing_name(self, tools):
        result = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"ip_address": "10.0.0.1"},
        ))
        assert result["error"] is True
        assert "name" in result["message"]

    @pytest.mark.asyncio
    async def test_blocked_fields(self, tools):
        result = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "x", "sys_updated_on": "2025-01-01"},
        ))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_empty_fields(self, tools):
        result = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={},
        ))
        assert result["error"] is True


# ── confirm_ci_create ───────────────────────────────────────────────

class TestConfirmCiCreate:
    @pytest.mark.asyncio
    async def test_full_create_flow(self, mock_client, tools):
        """Preview then confirm should execute the POST."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci_server",
            fields={"name": "new-server", "ip_address": "10.0.1.5"},
        ))
        token = preview["token"]

        result = _parse(await tools["confirm_ci_create"](token=token))
        assert result["success"] is True
        assert result["operation"] == "create"

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/api/now/table/cmdb_ci_server" in call_args.kwargs["path"]
        assert call_args.kwargs["json_body"]["name"] == "new-server"

    @pytest.mark.asyncio
    async def test_empty_token(self, tools):
        result = _parse(await tools["confirm_ci_create"](token=""))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_invalid_token(self, tools):
        result = _parse(await tools["confirm_ci_create"](token="bad"))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_token_retry_within_grace_period(self, mock_client, tools):
        """Retry within 60s grace period should return cached result."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "x"},
        ))
        token = preview["token"]

        result1 = _parse(await tools["confirm_ci_create"](token=token))
        assert result1["success"] is True

        # Second use within grace period returns same cached result
        result2 = _parse(await tools["confirm_ci_create"](token=token))
        assert result2["success"] is True
        assert result2 == result1

        # Should NOT have called post a second time
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_token_retry_after_grace_period_expires(self, mock_client, tools):
        """Retry after the 60s grace period should fail."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "x"},
        ))
        token = preview["token"]

        result1 = _parse(await tools["confirm_ci_create"](token=token))
        assert result1["success"] is True

        with patch("servicenow_cmdb_mcp.tools.mutations.time.time", return_value=time.time() + 120):
            result2 = _parse(await tools["confirm_ci_create"](token=token))
        assert result2["error"] is True
        assert "Invalid or expired" in result2["message"]

    @pytest.mark.asyncio
    async def test_wrong_operation_type(self, mock_client, tools):
        """An update token should not work with confirm_ci_create."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"name": "x"},
        ))
        token = preview["token"]

        result = _parse(await tools["confirm_ci_create"](token=token))
        assert result["error"] is True
        assert "update" in result["message"]

    @pytest.mark.asyncio
    async def test_service_now_error(self, mock_client, tools):
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "x"},
        ))
        mock_client.post.side_effect = SNPermissionError("Denied")
        result = _parse(await tools["confirm_ci_create"](token=preview["token"]))
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_token_preserved_on_retryable_error(self, mock_client, tools):
        """Retryable errors (429, 5xx) should preserve token for retry."""
        preview = _parse(await tools["preview_ci_create"](
            table="cmdb_ci", fields={"name": "new-ci"},
        ))
        token = preview["token"]

        mock_client.post.side_effect = RateLimitError("Rate limited", retry_after=5)
        result = _parse(await tools["confirm_ci_create"](token=token))
        assert result["error"] is True
        assert result["retry"] is True

        # Second attempt succeeds — token was preserved
        mock_client.post.side_effect = None
        mock_client.post.return_value = {"result": {**_ci_record(), "sys_id": "new123"}}
        result2 = _parse(await tools["confirm_ci_create"](token=token))
        assert result2["success"] is True

    @pytest.mark.asyncio
    async def test_wrong_operation_type_preserves_token(self, mock_client, tools):
        """Operation-type mismatch should NOT consume the token."""
        preview = _parse(await tools["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"name": "x"},
        ))
        token = preview["token"]

        # Wrong handler — should error but preserve
        result = _parse(await tools["confirm_ci_create"](token=token))
        assert result["error"] is True
        assert "update" in result["message"]

        # Token still works with correct handler
        result2 = _parse(await tools["confirm_ci_update"](token=token))
        assert result2["success"] is True
