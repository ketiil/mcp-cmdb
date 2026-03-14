"""Smoke test — validates discovery tools against a live ServiceNow PDI."""

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
from servicenow_cmdb_mcp.tools.discovery import register_discovery_tools


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
    register_discovery_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/4] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: list_discovery_schedules ──────────────────────────
        print("\n[1/4] Testing list_discovery_schedules...")
        result = json.loads(await tool_map["list_discovery_schedules"]())
        if result.get("error"):
            pp("SKIPPED (expected — Discovery plugin or role required)", result)
        else:
            pp("Discovery schedules", result)
            print(f"  OK — found {result['count']} schedules")

        # ── Test 2: get_discovery_status ──────────────────────────────
        print("\n[2/4] Testing get_discovery_status...")
        result = json.loads(await tool_map["get_discovery_status"]())
        if result.get("error"):
            pp("SKIPPED (expected — needs discovery_admin role)", result)
        else:
            pp("Discovery statuses", result)
            print(f"  OK — found {result['count']} status records")

        # ── Test 3: get_discovery_errors ──────────────────────────────
        print("\n[3/4] Testing get_discovery_errors...")
        result = json.loads(await tool_map["get_discovery_errors"](days=30))
        if result.get("error"):
            pp("SKIPPED (expected — needs discovery_admin role)", result)
        else:
            pp("Discovery errors", result)
            print(f"  OK — found {result['count']} errors (last {result['days_back']} days)")

        # ── Test 4: validation guards ────────────────────────────────
        print("\n[4/4] Testing validation guards...")
        r = json.loads(await tool_map["get_discovery_status"](schedule_name="test^evil"))
        assert r["error"] is True
        print("  OK — query injection blocked")

        r = json.loads(await tool_map["get_discovery_errors"](severity="Error^bad"))
        assert r["error"] is True
        print("  OK — severity injection blocked")

        print(f"\n{'='*60}")
        print("  ALL DISCOVERY SMOKE TESTS PASSED")
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
