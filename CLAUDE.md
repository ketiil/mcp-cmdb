# ServiceNow CMDB MCP Server

Python MCP server connecting Claude to ServiceNow CMDB via natural language. Uses FastMCP, OAuth 2.0, async httpx, pydantic. Targets ServiceNow Xanadu+.

## Tech Stack

- Python 3.11+, FastMCP (`mcp[cli]` >= 1.2.0), httpx (async), pydantic + pydantic-settings
- Transport: STDIO only
- Auth: OAuth 2.0 password grant against `/oauth_token.do`
- ServiceNow APIs: Table API, CMDB Instance API, Aggregate API, Data Model Navigator tables

## Project Structure

```
src/servicenow_cmdb_mcp/
├── server.py            # FastMCP server setup, entry point + check_connection tool
├── client.py            # Async ServiceNow REST client (auth, requests, pagination)
├── config.py            # Pydantic settings from env vars (SN_INSTANCE_URL, SN_CLIENT_ID, etc.)
├── cache.py             # In-memory metadata cache with TTL
├── redaction.py         # Credential pattern scrubbing in script bodies
├── errors.py            # Structured error types: ServiceNowError base, PermissionError, RateLimitError, etc.
├── tools/               # One module per domain, each exports register_*_tools(mcp, client[, cache])
│   ├── _utils.py        # Shared validators, pagination helpers, JSON serialization
│   ├── queries.py       # search_cis, query_cis_raw, get_ci_details, count_cis, suggest_table, list_ci_classes, describe_ci_class
│   ├── relationships.py # get_ci_relationships, get_dependency_tree, get_impact_summary, list_relationship_types, find_related_cis
│   ├── health.py        # find_orphan_cis, find_duplicate_cis, find_stale_cis, cmdb_health_summary
│   ├── mutations.py     # preview_ci_update/create → confirm_ci_update/create (two-phase)
│   ├── configurables.py # get_business_rules, get_flows, get_client_scripts, get_acls, get_script_includes, analyze_configurables
│   ├── discovery.py     # list_discovery_schedules, get_discovery_status, get_discovery_errors
│   ├── ire.py           # get_identification_rules, get_reconciliation_rules, explain_duplicate
│   └── imports.py       # list_data_sources, get_import_set_runs, get_transform_errors
├── resources/schema.py  # Dynamic MCP Resources + refresh_metadata_cache tool
└── prompts/workflows.py # MCP Prompts: Health Check, Impact Analysis, Troubleshoot CI, Audit Configurables
```

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run the MCP server (STDIO)
uv run servicenow-cmdb-mcp

# Run tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/test_queries.py -v

# Type check
uv run mypy src/

# Lint
uv run ruff check src/

# Fallback if uv run fails with "file in use" on Windows
.venv/Scripts/python.exe -m pytest tests/ -v
```

## Coding Rules

- `async def` for all tool handlers and client methods (httpx is async).
- Every tool MUST have a detailed docstring — FastMCP uses it for the tool schema.
- Tool params: `str`, `int`, `bool`, `list[str]`, `Literal[...]`. No `Any` in signatures.
- Use `Literal` for constrained params (e.g. `operational_status`, `severity`) — makes schema visible to agents.
- Return `str` (JSON-serialized) from all tools via `json.dumps(result, indent=2, default=str)`.

Tool schema stability (no versioning in MCP spec):
- **Never rename** a parameter — LLM clients cache tool definitions and renaming is a silent breaking change.
- **Never remove** a parameter — add a deprecation period or keep it as a no-op.
- **Never change** a parameter's type (e.g. `int` → `str`).
- **Only add** new optional parameters with sensible defaults — existing callers won't break.
- Changing the structure of return values is also a breaking change for downstream consumers.

ServiceNow query conventions:
- Always use `sysparm_fields` — never fetch full records.
- `STARTSWITH` for name filters, not `CONTAINS` (avoids full table scans).
- Paginate: `ORDERBYsys_created_on` + `sysparm_offset` + `sysparm_limit`.
- Aggregate API (`/api/now/stats/{table}`) for counts, not record fetches.
- Default limit 25, max 1000.

## Tool Registration Pattern

Each domain module exports a `register_*_tools()` function. Most take `(mcp, client)`, but `queries` and `relationships` also require `cache`:

```python
# In server.py
register_query_tools(mcp, client, cache)       # needs MetadataCache
register_relationship_tools(mcp, client, cache) # needs MetadataCache
register_health_tools(mcp, client)              # no cache
register_mutation_tools(mcp, client)            # no cache
# ... etc
```

Inside the module, use `@mcp.tool()` decorators with full docstrings and type hints.

## Tool Annotations

Add annotations to every tool. This controls auto-approval behavior in Claude Code:

- Read-only query tools: `readOnlyHint=True, destructiveHint=False, idempotentHint=True`
- Preview tools (no side effects): `readOnlyHint=True, destructiveHint=False, idempotentHint=True`
- Confirm tools (destructive but idempotent via `_completed_ops` cache): `readOnlyHint=False, destructiveHint=True, idempotentHint=True`

## Two-Phase Write Pattern

CI mutations use preview → confirm with a 5-minute token. Never write without the confirmation step.
- Retryable errors (429, 5xx, timeout): preserve token for retry.
- Permanent errors (403, 404): consume token.
- Operation-type mismatch: do NOT consume — token belongs to the other handler.

## Error Handling

All ServiceNow API errors MUST be caught and converted to structured error responses:

```python
{
    "error": True,
    "category": "PermissionError",
    "message": "Access denied to table cmdb_ci_server",
    "suggestion": "Check ACLs or try cmdb_ci instead.",
    "retry": False
}
```

Map HTTP status codes: 400→ValidationError, 401→AuthError, 403→PermissionError, 404→NotFoundError, 429→RateLimitError (include retry_after), 5xx→InstanceError. Also: SNTimeoutError (request timeout), PluginError (missing plugin). On 429, implement exponential backoff with jitter, max 3 retries.

## Security Rules

- Never echo tokens, credentials, or secrets in results or logs.
- Script bodies pass through `redaction.py` before returning.
- Credentials from env vars only. URL credentials stripped at `ServiceNowClient.__init__`.
- Input validation via `_utils.py` helpers (not pydantic — pydantic is only for `Settings`).
- `query_cis_raw` blocks `javascript:`, `gs.*`, `eval` via `_DANGEROUS_QUERY_PATTERNS`. Never use these in docstring examples.
- Log tool invocations with sanitized params. Never log secrets.

## Environment

All env vars use `SN_` prefix (set via `env_prefix` in pydantic-settings).

Required (no defaults — server won't start without them):
- `SN_INSTANCE_URL` — ServiceNow instance URL (e.g. `https://your-instance.service-now.com`)
- `SN_CLIENT_ID` — OAuth 2.0 client ID from Application Registry
- `SN_CLIENT_SECRET` — OAuth 2.0 client secret
- `SN_USERNAME` — Service account username
- `SN_PASSWORD` — Service account password

Optional (with defaults):
- `SN_REQUEST_TIMEOUT` — HTTP timeout in seconds (default: 30)
- `SN_CACHE_TTL` — Metadata cache TTL in seconds (default: 3600)
- `SN_MAX_RETRIES` — Retries on transient failures (default: 3)

## Metadata Caching

Data Model Navigator data (class hierarchy, fields, relationship types) is cached in-memory with a 1-hour TTL. The cache lives in `cache.py`. Tools in `resources/schema.py` read from cache. A `refresh_metadata_cache` utility tool forces a cache refresh.

## Key ServiceNow Tables

```
CMDB core:     cmdb_ci, cmdb_rel_ci, cmdb_rel_type, cmdb_rel_type_suggest
Schema:        sys_db_object, sys_dictionary, sys_documentation
Scripts:       sys_script, sys_script_client, sys_script_include
Flows:         sys_hub_flow
Transforms:    sys_transform_map, sys_import_set_row
Discovery:     discovery_schedule, discovery_status, discovery_log
IRE:           cmdb_ident_entry, cmdb_reconciliation_rule
Imports:       sys_data_source, sys_import_set, sys_import_set_run
ACLs:          sys_security_acl
```

## Testing

- Tests in `tests/test_<module>.py`, one per tools module.
- Fixture pattern: `mock_client` (AsyncMock), `tools` (registers on FastMCP, extracts `tool_map` dict).
- Parse tool output with `_parse(json_str)` → `dict`.
- Mock `client.get_records`, `client.get_record`, `client.get_aggregate` as needed.
- All async tests use `pytest.mark.asyncio`.

## Input Validation

- `_validate_table_name(t)` — format-only (ASCII, no traversal). Use for tools that inspect metadata *about* tables (business rules, ACLs, flows on `incident`, `alm_asset`, etc.).
- `_validate_cmdb_table(t)` — format + CMDB prefix restriction. Use for tools that query CI data directly (`search_cis`, `query_cis_raw`, `get_ci_details`, mutations).
- `_validate_sys_id(s)` — hex alphanumeric, no traversal. Use for all sys_id parameters.

## Reference

- Full architecture: see `ARCHITECTURE.md` in project root
- Table API: `GET /api/now/table/{table}`, CMDB API: `GET /api/now/cmdb/instance/{class}`, Stats API: `GET /api/now/stats/{table}`
