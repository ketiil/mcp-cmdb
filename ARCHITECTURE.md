# ServiceNow CMDB MCP Server — Architecture Document

**Version:** 1.1
**Date:** 2026-03-21
**Status:** Implemented — 30+ tools, 426+ tests passing

---

## 1. Overview

A Python MCP server that connects AI assistants (Claude Code, Claude Desktop) to a ServiceNow CMDB instance via natural language. It enables querying, troubleshooting, dependency analysis, CI lifecycle management, and inspection of configurables — all through the Model Context Protocol.

**What makes this different from existing ServiceNow MCPs:**

- Deep CMDB focus (not generic ITSM) with relationship traversal, health auditing, and dependency mapping
- Dynamic schema powered by the Data Model Navigator plugin (no hardcoded class hierarchies)
- Full configurable inspection (business rules, flows, client scripts, transform maps) with script body analysis and credential redaction
- Discovery, Identification/Reconciliation rules, and import set visibility
- Two-phase write confirmation pattern for safe mutations
- Tool annotations for smart auto-approval in Claude Code
- Built against OWASP MCP Top 10 security guidance

**ServiceNow baseline:** Xanadu and newer releases.

---

## 2. Technical Stack

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | FastMCP ecosystem, ServiceNow community alignment |
| MCP Framework | FastMCP (from `mcp[cli]` >= 1.2.0) | Decorator-based tools, auto schema generation, official SDK |
| HTTP Client | httpx (async) | Async-first, connection pooling, timeout control |
| Config/Validation | pydantic + pydantic-settings | Env var loading, strict typing, schema validation |
| Transport | STDIO | Local deployment for Claude Code / Claude Desktop |
| Auth | OAuth 2.0 password grant | Short-lived tokens, refresh support, ServiceNow native |

---

## 3. Authentication & Security

### 3.1 OAuth 2.0 Flow

Token lifecycle against ServiceNow `/oauth_token.do`:

1. On first tool call → password grant → obtain access + refresh token
2. Cache tokens in memory with expiry tracking
3. Refresh 60 seconds before expiry using refresh token
4. On refresh failure → fall back to password grant
5. On HTTP 401 → invalidate cache, re-authenticate, retry once

**Credentials source:** Environment variables only, injected at runtime. Never in config files, never in code.

```
SN_INSTANCE_URL=https://your-instance.service-now.com
SN_CLIENT_ID=<oauth-client-id>
SN_CLIENT_SECRET=<oauth-client-secret>
SN_USERNAME=<service-account-username>
SN_PASSWORD=<service-account-password>
```

### 3.2 Security Practices (OWASP MCP Top 10 aligned)

| OWASP MCP Risk | Mitigation |
|---|---|
| MCP01 — Token Mismanagement | OAuth with short-lived tokens; never echo tokens in tool results; no logging of credentials |
| MCP02 — Excessive Permissions | Single service account with least-privilege CMDB roles; write tools behind confirmation gate |
| Input Validation | `_utils.py` validators on all tool parameters before ServiceNow API calls (pydantic is only for `Settings`) |
| Credential Redaction | Regex-based scrubbing of patterns matching API keys, tokens, passwords, secrets in script bodies before returning to LLM |
| Audit Logging | Every tool invocation logged with timestamp, tool name, sanitized parameters (secrets stripped) |
| Rate Limiting | Respect ServiceNow HTTP 429; exponential backoff with jitter; surface as structured error |

### 3.3 Credential Redaction

Script bodies returned by configurable tools pass through a redaction filter before reaching the LLM:

```
Patterns redacted:
- API keys (gs.getProperty patterns with key/token/secret/password in name)
- Hardcoded passwords in string literals
- OAuth tokens/bearer tokens
- Base64-encoded credential blocks
- Connection strings with embedded passwords
```

Redacted content is replaced with `[REDACTED — credential pattern detected]`.

---

## 4. Performance

### 4.1 ServiceNow API Optimization

- **Field filtering:** Every query uses `sysparm_fields` to return only needed columns. Never fetch full records unless explicitly requested.
- **Stable pagination:** All paginated queries use `ORDERBYsys_created_on` (or specified field) with `sysparm_offset` + `sysparm_limit` to avoid inconsistent results.
- **Aggregate API for counts:** Use `/api/now/stats/{table}` with `sysparm_count=true` instead of fetching records to count them.
- **Prefer STARTSWITH over CONTAINS:** For name/string filters, use `STARTSWITH` by default (indexed) rather than `CONTAINS` (full table scan). Offer `CONTAINS` as explicit opt-in.
- **Default limit:** 25 records. Max configurable to 1000. Prevents accidental full-table dumps.

### 4.2 Metadata Caching

Data Model Navigator metadata (class hierarchy, field definitions, relationship types) changes rarely. Cache strategy:

- Lazy-loaded on first tool call that needs metadata (not at startup)
- TTL: configurable, default 1 hour
- Manual invalidation tool available (`refresh_metadata_cache`)
- Cache is in-memory (dict), no external dependencies

### 4.3 Connection Management

- Single `httpx.AsyncClient` instance with connection pooling
- Configurable request timeout (default 30s)
- Automatic retry on transient failures (429, 503) with exponential backoff + jitter
- Max 3 retries per request

---

## 5. Tool Domains

### 5.1 CI Queries

| Tool | Description | Annotations |
|---|---|---|
| `search_cis` | Structured search: class, name filter, status, OS, location. Builds encoded query from parameters. | readOnly, idempotent |
| `query_cis_raw` | Execute raw encoded query against any CMDB table. For advanced users. | readOnly, idempotent |
| `get_ci_details` | Full record for a CI by sys_id. | readOnly, idempotent |
| `count_cis` | Count CIs matching a query using Aggregate API. | readOnly, idempotent |
| `list_ci_classes` | List available CMDB classes from Data Model Navigator. | readOnly, idempotent |
| `describe_ci_class` | Get field definitions, descriptions, and suggested relationships for a class. | readOnly, idempotent |
| `suggest_table` | Given a natural language description ("linux servers", "network switches"), recommend the best CMDB table to query based on Data Model Navigator context metadata. | readOnly, idempotent |

### 5.2 Relationships & Dependencies

| Tool | Description | Annotations |
|---|---|---|
| `get_ci_relationships` | Get all relationships for a CI (upstream, downstream, or both). | readOnly, idempotent |
| `get_dependency_tree` | Walk the full dependency tree from a CI, with configurable depth and direction. | readOnly, idempotent |
| `list_relationship_types` | List all relationship types available in the instance. | readOnly, idempotent |
| `find_related_cis` | Find CIs related to a given CI by a specific relationship type (e.g., "Runs on", "Depends on"). | readOnly, idempotent |
| `get_impact_summary` | For a given CI, produce a summary of what services/applications would be impacted. | readOnly, idempotent |

### 5.3 CMDB Health & Troubleshooting

| Tool | Description | Annotations |
|---|---|---|
| `find_orphan_cis` | CIs with zero relationships in cmdb_rel_ci. | readOnly, idempotent |
| `find_duplicate_cis` | CIs with matching name, IP, serial number, or other key fields within a class. | readOnly, idempotent |
| `find_stale_cis` | CIs not updated by Discovery (or any source) in a configurable number of days. | readOnly, idempotent |
| `cmdb_health_summary` | Aggregate health metrics: total CIs by class, orphan count, duplicate count, stale count. | readOnly, idempotent |

### 5.4 CI Mutations

**Two-phase confirmation pattern:**

1. User requests a change (e.g., "update this server's status to retired")
2. Tool returns a **preview** of the change (current values vs. proposed values, affected record)
3. User confirms
4. Second tool call (`confirm_ci_update` / `confirm_ci_create`) executes the write

| Tool | Description | Annotations |
|---|---|---|
| `preview_ci_update` | Show what will change before executing. Returns diff. | readOnly, idempotent |
| `confirm_ci_update` | Execute a previously previewed update by confirmation token. | destructive, idempotent (via `_completed_ops` cache) |
| `preview_ci_create` | Show the record that will be created with defaults filled in. | readOnly, idempotent |
| `confirm_ci_create` | Execute a previously previewed creation. | destructive, idempotent (via `_completed_ops` cache) |

### 5.5 Configurables & Scripts

Detail tools return script bodies with credential redaction applied. `analyze_configurables` returns aggregate counts only.

| Tool | Description | Annotations |
|---|---|---|
| `get_business_rules` | Business rules for a given table (name, when, order, conditions, script). | readOnly, idempotent |
| `get_flows` | Flow Designer flows, optionally filtered by trigger table. | readOnly, idempotent |
| `get_client_scripts` | Client scripts for a table (type, field, script). | readOnly, idempotent |
| `get_acls` | ACL rules for a given table or field. | readOnly, idempotent |
| `analyze_configurables` | For a given CMDB table, count all touching configurables (BRs, flows, client scripts, ACLs) in one call. | readOnly, idempotent |

### 5.6 Discovery

| Tool | Description | Annotations |
|---|---|---|
| `list_discovery_schedules` | Active Discovery schedules with status and last run time. | readOnly, idempotent |
| `get_discovery_status` | Status of a specific Discovery schedule (running, completed, errors). | readOnly, idempotent |
| `get_discovery_errors` | Recent Discovery errors for a schedule or CI class. | readOnly, idempotent |

### 5.7 Identification & Reconciliation

| Tool | Description | Annotations |
|---|---|---|
| `get_identification_rules` | IRE identification rules for a CI class (matching criteria, priority). | readOnly, idempotent |
| `get_reconciliation_rules` | Reconciliation rules showing how attributes are prioritized across data sources. | readOnly, idempotent |
| `explain_duplicate` | Given two CIs that appear to be duplicates, explain why IRE did not merge them by comparing against identification rules. | readOnly, idempotent |

### 5.8 Data Sources & Import Sets

| Tool | Description | Annotations |
|---|---|---|
| `list_data_sources` | Data sources feeding the CMDB (Discovery, connectors, import sets). | readOnly, idempotent |
| `get_import_set_runs` | Recent import set run history with record counts and errors. | readOnly, idempotent |
| `get_transform_errors` | Errors from transform map executions. | readOnly, idempotent |

### 5.9 Utility

| Tool | Description | Annotations |
|---|---|---|
| `check_connection` | Verify ServiceNow connectivity, authenticated user, and roles. | readOnly, idempotent |
| `refresh_metadata_cache` | Force-refresh cached Data Model Navigator metadata. | NOT readOnly, idempotent |

---

## 6. MCP Resources

Dynamic, fetched from ServiceNow at startup and cached.

| Resource URI | Description | Source |
|---|---|---|
| `cmdb://schema/classes` | Full CMDB class hierarchy tree | `sys_db_object` filtered to cmdb_ci descendants + Data Model Navigator |
| `cmdb://schema/classes/{class_name}` | Field definitions for a specific class | `sys_dictionary` + Data Model Navigator descriptions |
| `cmdb://schema/relationship-types` | All relationship types with descriptions | `cmdb_rel_type` table |
| `cmdb://instance/metadata` | Instance info: ServiceNow version, installed CMDB plugins, node count | System properties API |

---

## 7. MCP Prompts

Reusable multi-step workflows the user can invoke.

### 7.1 CMDB Health Check

Runs: `cmdb_health_summary` → `find_orphan_cis` (top 10) → `find_duplicate_cis` (top 10) → `find_stale_cis` (top 10) → produces a structured health report with recommendations.

### 7.2 Impact Analysis

Input: CI name or sys_id. Runs: `get_ci_details` → `get_dependency_tree` (depth 3, upstream) → `get_ci_relationships` → produces impact assessment listing all affected services, applications, and infrastructure.

### 7.3 Troubleshoot CI

Input: CI name or sys_id. Runs: `get_ci_details` → `get_ci_relationships` → `analyze_configurables` (on the CI's table) → `get_identification_rules` → checks for staleness, orphan status, incomplete fields → produces diagnostic report.

### 7.4 Audit Configurables

Input: CMDB table name. Runs: `get_business_rules` → `get_flows` → `get_client_scripts` → `get_acls` → produces inventory with potential conflict analysis (e.g., multiple BRs on the same event with conflicting logic).

---

## 8. Error Handling

### 8.1 Error Classification

```
ServiceNowError (base class)
├── SNValidationError — bad query syntax, missing required fields (400)
├── SNPermissionError — ACL denied, insufficient role (403)
├── NotFoundError — sys_id or table doesn't exist (404)
├── RateLimitError — HTTP 429, includes retry_after
├── InstanceError — ServiceNow instance issue (5xx)
├── SNTimeoutError — request exceeded timeout
├── AuthError — OAuth grant failed, bad credentials (401)
└── PluginError — required plugin (Data Model Navigator) not installed
```

### 8.2 Error Response Format

Every error returned to the LLM includes:

```json
{
  "error": true,
  "category": "PermissionError",
  "message": "Access denied to table cmdb_ci_server",
  "suggestion": "The service account may lack the 'cmdb_read' role. Check ACLs for cmdb_ci_server or try querying the parent table cmdb_ci instead.",
  "retry": false
}
```

---

## 9. Project Structure

```
servicenow-cmdb-mcp/
├── CLAUDE.md                    # Claude Code instructions
├── pyproject.toml               # Project metadata, dependencies, entry point
├── README.md                    # User-facing documentation
├── .env.example                 # Template for environment variables
├── src/
│   └── servicenow_cmdb_mcp/
│       ├── __init__.py
│       ├── server.py            # FastMCP server setup, entry point + check_connection tool
│       ├── client.py            # ServiceNow REST client (auth, requests, pagination)
│       ├── config.py            # Pydantic settings, configuration
│       ├── cache.py             # Metadata cache with TTL
│       ├── redaction.py         # Credential pattern redaction
│       ├── errors.py            # ServiceNowError base + structured error types
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── _utils.py        # Shared validators, pagination helpers, JSON serialization
│       │   ├── queries.py       # CI query tools
│       │   ├── relationships.py # Dependency and relationship tools
│       │   ├── health.py        # CMDB health and troubleshooting tools
│       │   ├── mutations.py     # CI create/update with confirmation
│       │   ├── configurables.py # Scripts, flows, BRs, ACLs
│       │   ├── discovery.py     # Discovery schedule tools
│       │   ├── ire.py           # Identification & Reconciliation tools
│       │   └── imports.py       # Data sources and import set tools
│       ├── resources/
│       │   ├── __init__.py
│       │   └── schema.py        # Dynamic MCP Resources + refresh_metadata_cache tool
│       └── prompts/
│           ├── __init__.py
│           └── workflows.py     # MCP Prompt definitions
└── tests/
    ├── __init__.py
    ├── conftest.py              # Shared fixtures
    ├── test_queries.py
    ├── test_relationships.py
    ├── test_health.py
    ├── test_mutations.py
    ├── test_configurables.py
    ├── test_discovery.py
    ├── test_imports.py
    ├── test_ire.py
    ├── test_prompts.py
    ├── test_schema_resources.py
    └── test_server.py
```

---

## 10. Borrowed Patterns

| Pattern | Source | How we use it |
|---|---|---|
| Tool packaging by domain | echelon-ai-labs/servicenow-mcp | Tools organized into domain modules, registered via `register_*_tools()` functions |
| Dynamic schema discovery | Happy-Technologies-LLC/mcp-servicenow-nodejs | Runtime metadata from `sys_db_object` + `sys_dictionary` + Data Model Navigator |
| Resource URI patterns | michaelbuckner/servicenow-mcp | `cmdb://schema/classes`, `cmdb://schema/relationship-types`, etc. |
| CMDB troubleshooting concepts | mady22070/servicenow-mcp | Duplicate detection, health checks, configurable audit |

---

## 11. ServiceNow Prerequisites

For the MCP server to function, the target ServiceNow instance needs:

1. **Data Model Navigator plugin** installed (Store app, free)
2. **OAuth 2.0** enabled with an application registry entry (client ID + secret)
3. **Service account** with roles: `cmdb_read`, `itil` (for Discovery/IRE tables), and `cmdb_editor` (for mutation tools)
4. **REST API access** enabled for the service account (Web Service Access Only recommended)
5. **ACLs** granting read access to: `sys_db_object`, `sys_dictionary`, `sys_documentation`, `cmdb_rel_type`, `cmdb_rel_type_suggest`, `sys_script`, `sys_hub_flow`, `sys_script_client`, `sys_script_include`, `sys_transform_map`, `sysauto_script`, `discovery_status`, `cmdb_ident_entry`, `cmdb_reconciliation_rule`

---

## 12. Claude Code / Desktop Configuration

```json
{
  "mcpServers": {
    "servicenow-cmdb": {
      "command": "uv",
      "args": ["run", "servicenow-cmdb-mcp"],
      "env": {
        "SN_INSTANCE_URL": "https://your-instance.service-now.com",
        "SN_CLIENT_ID": "your-client-id",
        "SN_CLIENT_SECRET": "your-client-secret",
        "SN_USERNAME": "your-service-account",
        "SN_PASSWORD": "your-password"
      }
    }
  }
}
```

---

## 13. Open Items / Future Considerations

- **Service Mapping** integration (once scope warrants it)
- **CSDM alignment** tools (validate CI data against CSDM 5.0 domains)
- **Remote HTTP transport** for team deployment (deferred — STDIO only for now)
- **Multi-instance** support (connect to dev/test/prod simultaneously)
- **Event Management** integration (correlate CMDB CIs with alerts)

### 13.1 Planned Tools (not yet implemented)

| Tool | Domain | Description |
|---|---|---|
| `find_incomplete_cis` | Health | CIs missing mandatory or recommended attributes for their class. |
| `preview_relationship_create` | Mutations | Preview adding a relationship between two CIs. |
| `confirm_relationship_create` | Mutations | Execute relationship creation after preview. |
| `get_script_includes` | Configurables | Script includes, filterable by name. |
| `get_transform_maps` | Configurables | Transform maps for CMDB import sources. |
| `get_scheduled_jobs` | Configurables | Scheduled script executions related to CMDB. |
| `get_discovery_log` | Discovery | Discovery log entries for a specific CI or IP range. |
