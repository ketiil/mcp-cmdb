"""Smoke test — validates configurable inspection tools against a live ServiceNow PDI."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value

from mcp.server.fastmcp import FastMCP

from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.tools.configurables import register_configurable_tools


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
    register_configurable_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/6] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: get_business_rules ───────────────────────────────
        print("\n[1/6] Testing get_business_rules on cmdb_ci...")
        result = json.loads(await tool_map["get_business_rules"](table="cmdb_ci"))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Business rules", result)
        print(f"  OK — found {result['count']} business rules")

        # ── Test 2: get_client_scripts ───────────────────────────────
        print("\n[2/6] Testing get_client_scripts on cmdb_ci...")
        result = json.loads(await tool_map["get_client_scripts"](table="cmdb_ci"))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Client scripts", result)
        print(f"  OK — found {result['count']} client scripts")

        # ── Test 3: get_flows ────────────────────────────────────────
        print("\n[3/6] Testing get_flows on cmdb_ci...")
        result = json.loads(await tool_map["get_flows"](table="cmdb_ci"))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Flows", result)
        print(f"  OK — found {result['count']} flows")

        # ── Test 4: get_acls (SKIPPED — needs security_admin role) ──
        print("\n[4/5] Skipping get_acls (no security_admin role)")

        # ── Test 5: validation guards ────────────────────────────────
        print("\n[5/5] Testing validation guards...")
        r = json.loads(await tool_map["get_business_rules"](table=""))
        assert r["error"] is True
        print("  OK — empty table rejected")

        r = json.loads(await tool_map["get_business_rules"](table="bad/table"))
        assert r["error"] is True
        print("  OK — invalid table rejected")

        r = json.loads(await tool_map["get_acls"](table="cmdb_ci; DROP TABLE"))
        assert r["error"] is True
        print("  OK — injection attempt rejected")

        print(f"\n{'='*60}")
        print("  ALL CONFIGURABLE SMOKE TESTS PASSED")
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
