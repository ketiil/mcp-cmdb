"""Pydantic settings loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """ServiceNow CMDB MCP server configuration.

    All values are loaded from environment variables. No config files, no defaults
    for credentials — they must be explicitly set at runtime.
    """

    model_config = {"env_prefix": "SN_"}

    instance_url: str = Field(
        description="ServiceNow instance URL (e.g. https://your-instance.service-now.com)"
    )
    client_id: str = Field(description="OAuth 2.0 client ID from Application Registry")
    client_secret: str = Field(description="OAuth 2.0 client secret")
    username: str = Field(description="Service account username")
    password: str = Field(description="Service account password")

    request_timeout: int = Field(
        default=30,
        description="HTTP request timeout in seconds",
    )
    default_limit: int = Field(
        default=25,
        description="Default record limit for paginated queries",
    )
    max_limit: int = Field(
        default=1000,
        description="Maximum allowed record limit",
    )
    cache_ttl: int = Field(
        default=3600,
        description="Metadata cache TTL in seconds",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum retries on transient failures (429, 503)",
    )
