"""Smoke test — validates mutation tools against a live ServiceNow PDI.

Creates a test CI, updates it, then deletes it to clean up.
"""

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
from servicenow_cmdb_mcp.tools.mutations import register_mutation_tools


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
    register_mutation_tools(mcp, client)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    created_sys_id = None

    try:
        # ── Auth ─────────────────────────────────────────────────────
        print("\n[0/6] Authenticating...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 1: preview_ci_create ────────────────────────────────
        print("\n[1/6] Testing preview_ci_create...")
        result = json.loads(await tool_map["preview_ci_create"](
            table="cmdb_ci",
            fields={"name": "MCP-SMOKE-TEST-CI", "operational_status": "6"},
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Create preview", result)
        create_token = result["token"]
        print(f"  OK — got token: {create_token}")

        # ── Test 2: confirm_ci_create ────────────────────────────────
        print("\n[2/6] Testing confirm_ci_create...")
        result = json.loads(await tool_map["confirm_ci_create"](token=create_token))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Create confirmed", result)
        created_sys_id = result.get("sys_id") or result.get("created_record", {}).get("sys_id", "")
        print(f"  OK — created CI: {created_sys_id}")

        if not created_sys_id:
            print("  FAILED — no sys_id returned")
            return 1

        # ── Test 3: preview_ci_update ────────────────────────────────
        print("\n[3/6] Testing preview_ci_update...")
        result = json.loads(await tool_map["preview_ci_update"](
            sys_id=created_sys_id,
            table="cmdb_ci",
            fields={"operational_status": "2"},
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Update preview", result)
        update_token = result["token"]
        print(f"  OK — got token: {update_token}")

        # Verify diff
        diff = result["diff"]
        status_diff = next((d for d in diff if d["field"] == "operational_status"), None)
        assert status_diff is not None, "Expected operational_status in diff"
        assert status_diff["old_value"] == "6", f"Expected old=6, got {status_diff['old_value']}"
        assert status_diff["new_value"] == "2"
        print("  OK — diff verified")

        # ── Test 4: confirm_ci_update ────────────────────────────────
        print("\n[4/6] Testing confirm_ci_update...")
        result = json.loads(await tool_map["confirm_ci_update"](token=update_token))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Update confirmed", result)
        print(f"  OK — CI updated")

        # ── Test 5: token reuse blocked ──────────────────────────────
        print("\n[5/6] Testing token reuse is blocked...")
        result = json.loads(await tool_map["confirm_ci_update"](token=update_token))
        assert result.get("error") is True, "Reused token should fail"
        print(f"  OK — reuse blocked: {result['message']}")

        # ── Test 6: validation errors ────────────────────────────────
        print("\n[6/6] Testing validation guards...")
        r = json.loads(await tool_map["preview_ci_update"](
            sys_id="", table="cmdb_ci", fields={"name": "x"},
        ))
        assert r["error"] is True
        print("  OK — empty sys_id rejected")

        r = json.loads(await tool_map["preview_ci_update"](
            sys_id=CI_A, table="cmdb_ci", fields={"sys_id": "hack"},
        ))
        assert r["error"] is True
        print("  OK — blocked field rejected")

        r = json.loads(await tool_map["preview_ci_create"](
            table="cmdb_ci", fields={"ip_address": "10.0.0.1"},
        ))
        assert r["error"] is True
        print("  OK — missing name rejected")

        print(f"\n{'='*60}")
        print("  ALL MUTATION SMOKE TESTS PASSED")
        print(f"{'='*60}")
        return 0

    except Exception as e:
        print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Clean up: delete the test CI
        if created_sys_id:
            print(f"\n[CLEANUP] Deleting test CI {created_sys_id}...")
            try:
                await client.delete(f"/api/now/table/cmdb_ci/{created_sys_id}")
                print("  Deleted.")
            except Exception as e:
                print(f"  Cleanup failed: {e}")
        await client.close()


CI_A = "a" * 32

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
