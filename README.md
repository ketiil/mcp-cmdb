# ServiceNow CMDB MCP Server

A Python MCP server that connects AI assistants (Claude Code, Claude Desktop, Cursor) to a ServiceNow CMDB instance via natural language. Enables querying, dependency analysis, health auditing, CI lifecycle management, and configurable inspection — all through the Model Context Protocol.

## What makes this different

- Deep CMDB focus with relationship traversal, impact analysis, path finding, and dependency mapping
- `display_value` support — reference fields return human-readable names, not opaque sys_ids
- ASCII tree visualization for dependency graphs directly in the terminal
- Dynamic schema powered by the Data Model Navigator plugin (no hardcoded class hierarchies)
- Full configurable inspection (business rules, flows, flow logic, client scripts, script includes, ACLs) with credential redaction
- Discovery, IRE rules, and import set visibility
- Two-phase write confirmation for safe CI mutations
- Rich server instructions that guide LLM behavior (workflow patterns, smart defaults, disambiguation)
- Tool annotations for smart auto-approval in Claude Code

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- ServiceNow instance (Xanadu+) with an OAuth 2.0 application and service account

## Setup

1. Clone the repository and install dependencies:

```bash
uv sync
```

2. Copy `.env.example` to `.env` and fill in your ServiceNow credentials:

```bash
cp .env.example .env
```

```
SN_INSTANCE_URL=https://your-instance.service-now.com
SN_CLIENT_ID=your-oauth-client-id
SN_CLIENT_SECRET=your-oauth-client-secret
SN_USERNAME=your-service-account
SN_PASSWORD=your-password
```

3. Run the server:

```bash
uv run servicenow-cmdb-mcp
```

## Claude Code integration

Add to your `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "servicenow-cmdb": {
      "command": "uv",
      "args": ["run", "servicenow-cmdb-mcp"],
      "cwd": "/path/to/mcp-cmdb",
      "env": {
        "SN_INSTANCE_URL": "https://your-instance.service-now.com",
        "SN_CLIENT_ID": "your-oauth-client-id",
        "SN_CLIENT_SECRET": "your-oauth-client-secret",
        "SN_USERNAME": "your-service-account",
        "SN_PASSWORD": "your-password"
      }
    }
  }
}
```

## Recommended ServiceNow roles

The service account needs these roles for full functionality:

| Role | What it unlocks |
|---|---|
| `cmdb_read` | CMDB CI queries, relationships, health tools |
| `itil` | Standard ITSM tables, basic platform access |
| `fd_read` | Flow Designer read access (flow details, triggers) |
| `personalize` | Client scripts, UI policies |

Optional roles for deeper inspection:

| Role | What it unlocks |
|---|---|
| `flow_designer` | Flow overview listing |
| `script_include_admin` | Script include read access |
| `business_rule_admin` | Business rule scripts |

## Tools (38 total)

| Domain | Tools | Description |
|---|---|---|
| Queries | `search_cis`, `query_cis_raw`, `get_ci_details`, `count_cis`, `suggest_table`, `list_ci_classes`, `describe_ci_class` | Search, inspect, and explore CMDB classes. Supports `display_value` for human-readable reference fields |
| Relationships | `get_ci_relationships`, `find_related_cis`, `list_relationship_types`, `get_dependency_tree`, `get_impact_summary`, `find_ci_path` | Traverse CI relationships, visualize dependency trees (ASCII format), find shortest paths between CIs. Supports `class_filter` to focus on specific CI types |
| Health | `find_orphan_cis`, `find_duplicate_cis`, `find_stale_cis`, `cmdb_health_summary` | Audit CMDB data quality |
| Mutations | `preview_ci_create`, `confirm_ci_create`, `preview_ci_update`, `confirm_ci_update` | Safe two-phase CI writes |
| Configurables | `get_business_rules`, `get_flows`, `get_flow_details`, `get_client_scripts`, `get_acls`, `get_script_includes`, `analyze_configurables` | Inspect automation, flow logic, and security config |
| Discovery | `list_discovery_schedules`, `get_discovery_status`, `get_discovery_errors` | Monitor discovery operations |
| IRE | `get_identification_rules`, `get_reconciliation_rules`, `explain_duplicate` | Inspect identification and reconciliation |
| Imports | `list_data_sources`, `get_import_set_runs`, `get_transform_errors` | Monitor data imports and transforms |
| Utilities | `check_connection`, `refresh_metadata_cache`, `_diag_probe_table` | Connectivity check, cache management, table access diagnostics |

## Key features

### Display values
Query tools support `display_value="true"` to return human-readable names instead of sys_ids for reference fields (location, assigned_to, company, etc.).

### Tree visualization
`get_dependency_tree` supports `format="ascii_tree"` to return pre-rendered text trees, and `class_filter` to show only specific CI types (e.g., servers only, no disks/memory).

### Flow inspection
`get_flow_details` parses the internal `label_cache` from Flow Designer to show flow triggers, steps, referenced tables, and data flow — without needing admin access.

### Path finding
`find_ci_path` finds the shortest relationship path between any two CIs using BFS traversal.

## Development

```bash
# Run unit tests
uv run pytest tests/ -v

# Run smoke tests (requires live ServiceNow instance)
uv run python smoke_tests/smoke_test.py

# Type check
uv run mypy src/

# Lint
uv run ruff check src/

# Fallback if uv run fails with "file in use" on Windows
.venv/Scripts/python.exe -m pytest tests/ -v
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design document covering authentication, security, tool patterns, and API usage.

## Tech stack

- **FastMCP** (`mcp[cli]`) — MCP server framework with decorator-based tool registration
- **httpx** — Async HTTP client for ServiceNow REST APIs
- **pydantic / pydantic-settings** — Configuration and validation
- **OAuth 2.0** — Password grant against ServiceNow `/oauth_token.do`
- **STDIO transport** — Local deployment for Claude Code / Claude Desktop / Cursor
