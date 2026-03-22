"""Structured error types and HTTP status code mapping."""

from __future__ import annotations

import json


class ServiceNowError(Exception):
    """Base structured error returned to the LLM.

    Extends Exception so it can be raised/caught in the client layer,
    and provides to_json() for structured tool error responses.
    """

    def __init__(self, category: str, message: str, suggestion: str, retry: bool) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.suggestion = suggestion
        self.retry = retry
        self.retry_after_seconds: int | None = None

    def to_json(self) -> str:
        """Serialize to JSON string for tool responses."""
        data = {
            "error": True,
            "category": self.category,
            "message": self.message,
            "suggestion": self.suggestion,
            "retry": self.retry,
        }
        if self.retry_after_seconds is not None:
            data["retry_after_seconds"] = self.retry_after_seconds
        return json.dumps(data, indent=2)


class SNValidationError(ServiceNowError):
    """Bad query syntax or missing required fields (HTTP 400)."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(
            category="ValidationError",
            message=message,
            suggestion=suggestion or "Check query syntax and required fields.",
            retry=False,
        )


class AuthError(ServiceNowError):
    """OAuth grant failed or bad credentials (HTTP 401)."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(
            category="AuthError",
            message=message,
            suggestion=suggestion or "Verify SN_CLIENT_ID, SN_CLIENT_SECRET, SN_USERNAME, and SN_PASSWORD.",
            retry=False,
        )


class SNPermissionError(ServiceNowError):
    """ACL denied or insufficient role (HTTP 403)."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(
            category="PermissionError",
            message=message,
            suggestion=suggestion or "Check ACLs or try a parent table with broader access.",
            retry=False,
        )


class NotFoundError(ServiceNowError):
    """Table or sys_id does not exist (HTTP 404)."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(
            category="NotFoundError",
            message=message,
            suggestion=suggestion or "Verify the table name or sys_id exists.",
            retry=False,
        )


class RateLimitError(ServiceNowError):
    """HTTP 429 — includes retry_after hint."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        suggestion = "ServiceNow rate limit hit."
        if retry_after:
            suggestion += f" Retry after {retry_after} seconds."
        super().__init__(
            category="RateLimitError",
            message=message,
            suggestion=suggestion,
            retry=True,
        )
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after


class InstanceError(ServiceNowError):
    """ServiceNow 5xx server error."""

    def __init__(self, message: str) -> None:
        super().__init__(
            category="InstanceError",
            message=message,
            suggestion="ServiceNow instance may be experiencing issues. Try again shortly.",
            retry=True,
        )
        self.retry_after_seconds = 5


class SNTimeoutError(ServiceNowError):
    """Request exceeded configured timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(
            category="TimeoutError",
            message=message,
            suggestion="Request timed out. Try a more specific query or increase timeout.",
            retry=True,
        )
        self.retry_after_seconds = 10


class PluginError(ServiceNowError):
    """Required plugin not installed on the instance."""

    def __init__(self, message: str) -> None:
        super().__init__(
            category="PluginError",
            message=message,
            suggestion="Ensure the Data Model Navigator plugin is installed on the instance.",
            retry=False,
        )


# HTTP status code → error class mapping
STATUS_CODE_MAP: dict[int, type[ServiceNowError]] = {
    400: SNValidationError,
    401: AuthError,
    403: SNPermissionError,
    404: NotFoundError,
    429: RateLimitError,
}


def error_from_status(status_code: int, message: str, retry_after: int | None = None) -> ServiceNowError:
    """Create the appropriate error type from an HTTP status code."""
    if status_code == 400:
        return SNValidationError(message)
    if status_code == 401:
        return AuthError(message)
    if status_code == 403:
        return SNPermissionError(message)
    if status_code == 404:
        return NotFoundError(message)
    if status_code == 429:
        return RateLimitError(message, retry_after=retry_after)
    if 500 <= status_code < 600:
        return InstanceError(message)
    return ServiceNowError(
        category="UnknownError",
        message=message,
        suggestion="Unexpected error. Check the ServiceNow instance logs.",
        retry=False,
    )
