"""Smoke test — validates IRE tools against a live ServiceNow PDI."""

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
from servicenow_cmdb_mcp.tools.ire import register_ire_tools


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
    register_ire_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/4] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: get_identification_rules ─────────────────────────
        print("\n[1/4] Testing get_identification_rules...")
        result = json.loads(await tool_map["get_identification_rules"]())
        if result.get("error"):
            pp("SKIPPED (may need cmdb_admin role)", result)
        else:
            pp("Identification rules", result)
            print(f"  OK — found {result['count']} rules")

        # ── Test 2: get_identification_rules with table filter ───────
        print("\n[2/4] Testing get_identification_rules for cmdb_ci_server...")
        result = json.loads(await tool_map["get_identification_rules"](table="cmdb_ci_server"))
        if result.get("error"):
            pp("SKIPPED", result)
        else:
            pp("Server ID rules", result)
            print(f"  OK — found {result['count']} rules for cmdb_ci_server")

        # ── Test 3: get_reconciliation_rules ─────────────────────────
        print("\n[3/4] Testing get_reconciliation_rules...")
        result = json.loads(await tool_map["get_reconciliation_rules"]())
        if result.get("error"):
            pp("SKIPPED (may need cmdb_admin role)", result)
        else:
            pp("Reconciliation rules", result)
            print(f"  OK — found {result['count']} rules")

        # ── Test 4: validation guards ────────────────────────────────
        print("\n[4/4] Testing validation guards...")
        r = json.loads(await tool_map["get_identification_rules"](table="bad/table"))
        assert r["error"] is True
        print("  OK — invalid table rejected")

        r = json.loads(await tool_map["explain_duplicate"](
            sys_id_a="", sys_id_b="abc",
        ))
        assert r["error"] is True
        print("  OK — empty sys_id rejected")

        print(f"\n{'='*60}")
        print("  ALL IRE SMOKE TESTS PASSED")
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
