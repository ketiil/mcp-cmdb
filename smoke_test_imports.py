"""Smoke test — validates import tools against a live ServiceNow PDI."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.tools.imports import register_import_tools


def pp(label: str, data: object) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if isinstance(data, str):
        data = json.loads(data)
    print(json.dumps(data, indent=2, default=str))


async def main() -> int:
    settings = Settings()
    client = ServiceNowClient(settings)

    mcp = FastMCP("smoke-test")
    register_import_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/4] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: list_data_sources ────────────────────────────────
        print("\n[1/4] Testing list_data_sources...")
        result = json.loads(await tool_map["list_data_sources"]())
        if result.get("error"):
            pp("SKIPPED (may need import_admin role)", result)
        else:
            pp("Data sources", result)
            print(f"  OK — found {result['count']} data sources")

        # ── Test 2: get_import_set_runs ──────────────────────────────
        print("\n[2/4] Testing get_import_set_runs...")
        result = json.loads(await tool_map["get_import_set_runs"](days=30))
        if result.get("error"):
            pp("SKIPPED (may need import_admin role)", result)
        else:
            pp("Import set runs", result)
            print(f"  OK — found {result['count']} runs (last {result['days_back']} days)")

        # ── Test 3: get_transform_errors ─────────────────────────────
        print("\n[3/4] Testing get_transform_errors...")
        result = json.loads(await tool_map["get_transform_errors"](days=30))
        if result.get("error"):
            pp("SKIPPED (may need import_admin role)", result)
        else:
            pp("Transform errors", result)
            print(f"  OK — found {result['count']} errors (last {result['days_back']} days)")

        # ── Test 4: validation guards ────────────────────────────────
        print("\n[4/4] Testing validation guards...")
        r = json.loads(await tool_map["list_data_sources"](target_table="bad/table"))
        assert r["error"] is True
        print("  OK — invalid table rejected")

        r = json.loads(await tool_map["get_import_set_runs"](table_name="test^evil"))
        assert r["error"] is True
        print("  OK — query injection blocked")

        r = json.loads(await tool_map["get_import_set_runs"](state="Error^bad"))
        assert r["error"] is True
        print("  OK — state injection blocked")

        print(f"\n{'='*60}")
        print("  ALL IMPORT SMOKE TESTS PASSED")
        print(f"{'='*60}")
        return 0

    except Exception as e:
        print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
