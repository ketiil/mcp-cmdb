"""Smoke test — validates OAuth, Table API, Aggregate API, and sys_db_object access."""

import asyncio
import json
import os
import sys

# Load .env manually to handle special characters safely
from pathlib import Path

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value

from servicenow_cmdb_mcp.config import Settings
from servicenow_cmdb_mcp.client import ServiceNowClient


def pp(label: str, data: object) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2, default=str))


async def main() -> int:
    settings = Settings()
    client = ServiceNowClient(settings)

    try:
        # ── Test 1: OAuth authentication ─────────────────────────────
        print("\n[1/5] Testing OAuth 2.0 password grant...")
        token = await client._ensure_token()
        print(f"  OK — got access token ({len(token)} chars)")

        # ── Test 2: Table API — search CIs ───────────────────────────
        print("\n[2/5] Testing Table API — search cmdb_ci (limit 5)...")
        records = await client.get_records(
            table="cmdb_ci",
            fields=["sys_id", "name", "sys_class_name", "operational_status"],
            limit=5,
        )
        pp("Sample CIs", records)
        print(f"  OK — got {len(records)} records")

        # ── Test 3: Aggregate API — count CIs ────────────────────────
        print("\n[3/5] Testing Aggregate API — count all CIs...")
        agg = await client.get_aggregate(table="cmdb_ci")
        pp("Aggregate result", agg)
        print(f"  OK — aggregate response received")

        # ── Test 4: sys_db_object — list CMDB classes ────────────────
        print("\n[4/5] Testing sys_db_object — list child classes of cmdb_ci...")
        classes = await client.get_records(
            table="sys_db_object",
            query="super_class.name=cmdb_ci",
            fields=["name", "label", "super_class"],
            limit=10,
            order_by="ORDERBYname",
        )
        pp("CMDB classes (first 10)", classes)
        print(f"  OK — got {len(classes)} classes")

        # ── Test 5: Reference field format check ─────────────────────
        print("\n[5/5] Checking reference field format (super_class)...")
        if classes:
            sc = classes[0].get("super_class")
            print(f"  super_class raw value: {sc!r}")
            print(f"  Type: {type(sc).__name__}")
            if isinstance(sc, dict):
                print("  Format: OBJECT (has 'value' key)")
            else:
                print("  Format: STRING (plain sys_id)")

        print("\n" + "="*60)
        print("  ALL SMOKE TESTS PASSED")
        print("="*60)
        return 0

    except Exception as e:
        print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
