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
├── server.py            # FastMCP server setup + entry point
├── client.py            # Async ServiceNow REST client (auth, requests, pagination)
├── config.py            # Pydantic settings from env vars (SN_INSTANCE_URL, SN_CLIENT_ID, etc.)
├── cache.py             # In-memory metadata cache with TTL
├── redaction.py         # Credential pattern scrubbing in script bodies
├── errors.py            # Structured error types: ClientError, PermissionError, RateLimitError, etc.
├── tools/               # One module per domain, each exports register_*_tools(mcp, client)
│   ├── queries.py       # search_cis, query_cis_raw, get_ci_details, count_cis, suggest_table
│   ├── relationships.py # get_ci_relationships, get_dependency_tree, get_impact_summary
│   ├── health.py        # find_orphan_cis, find_duplicate_cis, find_stale_cis, cmdb_health_summary
│   ├── mutations.py     # preview_ci_update/create → confirm_ci_update/create (two-phase)
│   ├── configurables.py # get_business_rules, get_flows, get_client_scripts, get_acls, analyze_configurables
│   ├── discovery.py     # list_discovery_schedules, get_discovery_errors
│   ├── ire.py           # get_identification_rules, get_reconciliation_rules, explain_duplicate
│   └── imports.py       # list_data_sources, get_import_set_runs, get_transform_errors
├── resources/schema.py  # Dynamic MCP Resources from Data Model Navigator
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
```

## Coding Rules

- Use `async def` for all tool handlers and client methods. httpx client is async.
- Every tool function MUST have a detailed docstring — FastMCP uses it to generate the tool schema.
- Tool parameters use Python type hints. Use `str`, `int`, `bool`, `list[str]`. No `Any`.
- Return `str` (JSON-serialized) from all tools. Use `json.dumps(result, indent=2, default=str)`.
- All ServiceNow queries MUST use `sysparm_fields` — never fetch full records by default.
- Use `STARTSWITH` for name filters, not `CONTAINS` (avoids full table scans).
- Paginate with `ORDERBYsys_created_on` + `sysparm_offset` + `sysparm_limit`.
- Use Aggregate API (`/api/now/stats/{table}`) for counts, not record fetches.
- Default limit is 25 records. Max 1000. Always respect this.

## Tool Registration Pattern

Each domain module exports a `register_*_tools(mcp, client)` function. Server.py calls them all:

```python
# In server.py
from servicenow_cmdb_mcp.tools.queries import register_query_tools
register_query_tools(mcp, client)
```

Inside the module, use `@mcp.tool()` decorators with full docstrings and type hints.

## Tool Annotations

Add annotations to every tool. This controls auto-approval behavior in Claude Code:

- Read-only query tools: `readOnlyHint=True, destructiveHint=False, idempotentHint=True`
- Write/mutation tools: `readOnlyHint=False, destructiveHint=True, idempotentHint=False`

## Two-Phase Write Pattern

CI mutations use preview → confirm. The preview tool returns a diff and a confirmation token (a short random string). The confirm tool requires that token. Never execute writes without the confirmation step.

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

Map HTTP status codes: 400→ValidationError, 401→AuthError, 403→PermissionError, 404→NotFoundError, 429→RateLimitError (include retry_after). On 429, implement exponential backoff with jitter, max 3 retries.

## Security Rules

- NEVER echo OAuth tokens, credentials, or secrets in tool results or logs.
- ALL script bodies (business rules, flows, client scripts, script includes) pass through `redaction.py` before returning. Redact patterns matching API keys, passwords, tokens, connection strings.
- Credentials load from env vars ONLY. No hardcoded values. No config files.
- Validate all tool input parameters with pydantic before hitting ServiceNow API.
- Log every tool invocation (tool name, timestamp, sanitized params). Never log secrets.

## Metadata Caching

Data Model Navigator data (class hierarchy, fields, relationship types) is cached in-memory with a 1-hour TTL. The cache lives in `cache.py`. Tools in `resources/schema.py` read from cache. A `refresh_metadata_cache` utility tool forces a cache refresh.

## Key ServiceNow Tables

```
CMDB core:     cmdb_ci, cmdb_rel_ci, cmdb_rel_type, cmdb_rel_type_suggest
Schema:        sys_db_object, sys_dictionary, sys_documentation
Scripts:       sys_script, sys_script_client, sys_script_include
Flows:         sys_hub_flow
Transforms:    sys_transform_map
Schedules:     sysauto_script
Discovery:     discovery_status, discovery_log
IRE:           cmdb_ident_entry, cmdb_reconciliation_rule
Imports:       sys_import_set, sys_import_set_run
ACLs:          sys_security_acl
```

## Reference

- Full architecture: see `ARCHITECTURE.md` in project root
- ServiceNow Table API: `GET /api/now/table/{table}`
- ServiceNow CMDB Instance API: `GET /api/now/cmdb/instance/{class}`
- ServiceNow Aggregate API: `GET /api/now/stats/{table}`
- MCP spec: https://modelcontextprotocol.io/specification/2025-11-25
- FastMCP docs: https://github.com/modelcontextprotocol/python-sdk
