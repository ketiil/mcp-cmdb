"""Smoke test — validates health tools against a live ServiceNow PDI."""

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
from servicenow_cmdb_mcp.tools.health import register_health_tools


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
    register_health_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/4] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: cmdb_health_summary ──────────────────────────────
        print("\n[1/4] Testing cmdb_health_summary...")
        result = json.loads(await tool_map["cmdb_health_summary"]())
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Health summary", result)
        print(f"  OK — {result['total_count']} total CIs, "
              f"{result['stale_count']} stale, "
              f"{result['missing_name_count']} missing name")

        # ── Test 2: find_stale_cis ───────────────────────────────────
        print("\n[2/4] Testing find_stale_cis (90 days)...")
        result = json.loads(await tool_map["find_stale_cis"](days=90, limit=5))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Stale CIs (first 5)", result)
        print(f"  OK — {result['count']} returned, {result['total_stale']} total stale")

        # ── Test 3: find_duplicate_cis ───────────────────────────────
        print("\n[3/4] Testing find_duplicate_cis (by name)...")
        result = json.loads(await tool_map["find_duplicate_cis"](limit=5))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Duplicate CIs (first 5 groups)", result)
        print(f"  OK — {result['duplicate_group_count']} groups returned, "
              f"{result['total_duplicate_groups']} total")

        # ── Test 4: find_orphan_cis ──────────────────────────────────
        print("\n[4/4] Testing find_orphan_cis (limit 5)...")
        result = json.loads(await tool_map["find_orphan_cis"](limit=5))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Orphan CIs (first 5)", result)
        print(f"  OK — {result['count']} orphans found")

        print(f"\n{'='*60}")
        print("  ALL HEALTH SMOKE TESTS PASSED")
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
