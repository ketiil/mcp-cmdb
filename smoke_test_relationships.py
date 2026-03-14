"""Smoke test — validates relationship tools against a live ServiceNow PDI."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env manually to handle special characters safely
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value

from servicenow_cmdb_mcp.cache import MetadataCache
from servicenow_cmdb_mcp.client import ServiceNowClient
from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.tools.relationships import register_relationship_tools
from mcp.server.fastmcp import FastMCP


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
    cache = MetadataCache(ttl=3600)

    # Register tools on a FastMCP instance to get the tool functions
    mcp = FastMCP("smoke-test")
    register_relationship_tools(mcp, client, cache)
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.fn.__name__] = tool.fn

    try:
        # ── Step 0: Auth + find a CI with relationships ──────────────
        print("\n[0/6] Authenticating and finding a CI with relationships...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # Find a CI that has at least one relationship
        rel_records = await client.get_records(
            table="cmdb_rel_ci",
            fields=["sys_id", "parent", "child", "type"],
            limit=1,
        )
        if not rel_records:
            print("  SKIP — no relationships found in cmdb_rel_ci. PDI may be empty.")
            print("  All relationship tools require existing data to test meaningfully.")
            return 0

        # Extract a CI sys_id from the first relationship (use parent)
        from servicenow_cmdb_mcp.client import resolve_ref
        parent_id = resolve_ref(rel_records[0].get("parent", ""))
        child_id = resolve_ref(rel_records[0].get("child", ""))
        test_ci = parent_id or child_id
        print(f"  Found relationship. Using CI sys_id: {test_ci}")

        # ── Test 1: list_relationship_types ──────────────────────────
        print("\n[1/6] Testing list_relationship_types...")
        result = json.loads(await tool_map["list_relationship_types"](limit=5))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Relationship types (first 5)", result)
        print(f"  OK — got {result['count']} types")
        rel_type_name = result["relationship_types"][0]["name"] if result["relationship_types"] else None

        # ── Test 2: get_ci_relationships (both) ──────────────────────
        print("\n[2/6] Testing get_ci_relationships (direction=both)...")
        result = json.loads(await tool_map["get_ci_relationships"](
            ci_sys_id=test_ci, direction="both", limit=10,
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("CI relationships", result)
        print(f"  OK — got {result['count']} relationships")

        # ── Test 3: get_ci_relationships (upstream only) ─────────────
        print("\n[3/6] Testing get_ci_relationships (direction=upstream)...")
        result = json.loads(await tool_map["get_ci_relationships"](
            ci_sys_id=test_ci, direction="upstream", limit=5,
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        print(f"  OK — got {result['count']} upstream relationships")

        # ── Test 4: get_dependency_tree ──────────────────────────────
        print("\n[4/6] Testing get_dependency_tree (depth=2)...")
        result = json.loads(await tool_map["get_dependency_tree"](
            ci_sys_id=test_ci, direction="upstream", max_depth=2, limit_per_level=5,
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Dependency tree", result)
        child_count = len(result["tree"].get("children", []))
        print(f"  OK — root has {child_count} children")

        # ── Test 5: find_related_cis ─────────────────────────────────
        print("\n[5/6] Testing find_related_cis...")
        if rel_type_name:
            result = json.loads(await tool_map["find_related_cis"](
                ci_sys_id=test_ci, rel_type=rel_type_name, direction="both", limit=5,
            ))
            if result.get("error"):
                pp("ERROR", result)
                return 1
            pp(f"Related CIs (type={rel_type_name})", result)
            print(f"  OK — got {result['count']} related CIs")
        else:
            print("  SKIP — no relationship type available to filter by")

        # ── Test 6: get_impact_summary ───────────────────────────────
        print("\n[6/6] Testing get_impact_summary (depth=2)...")
        result = json.loads(await tool_map["get_impact_summary"](
            ci_sys_id=test_ci, max_depth=2,
        ))
        if result.get("error"):
            pp("ERROR", result)
            return 1
        pp("Impact summary", result)
        print(f"  OK — {result['total_impacted']} impacted CIs, "
              f"{len(result['impacted_services'])} services")

        # ── Validation checks ────────────────────────────────────────
        print("\n[VALIDATION] Testing edge cases against live API...")

        # Empty sys_id
        result = json.loads(await tool_map["get_ci_relationships"](""))
        assert result["error"] is True and result["category"] == "ValidationError", \
            "Empty sys_id should return ValidationError"
        print("  OK — empty sys_id returns ValidationError")

        # Bad direction
        result = json.loads(await tool_map["get_ci_relationships"](test_ci, direction="invalid"))
        assert result["error"] is True and result["category"] == "ValidationError", \
            "Invalid direction should return ValidationError"
        print("  OK — invalid direction returns ValidationError")

        # Query injection
        result = json.loads(await tool_map["find_related_cis"](
            test_ci, rel_type="foo^ORactive=true",
        ))
        assert result["error"] is True and result["category"] == "ValidationError", \
            "Query injection should be blocked"
        print("  OK — query injection blocked")

        print(f"\n{'='*60}")
        print("  ALL RELATIONSHIP SMOKE TESTS PASSED")
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
