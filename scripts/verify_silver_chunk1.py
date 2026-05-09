"""Quick verification of the 5 new silver tables built in Chunk 1 (S10).

Rows-only sanity check + quarantine totals. Auth: az login session.
"""

from __future__ import annotations

import sys
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

WORKSPACE_HOST = "https://adb-7405617631498102.2.azuredatabricks.net"


def run_sql(w: WorkspaceClient, warehouse_id: str, sql: str) -> list[list]:
    s = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="30s",
    )
    while s.status.state in (StatementState.PENDING, StatementState.RUNNING):
        s = w.statement_execution.get_statement(s.statement_id)
    if s.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {s.status.error.message if s.status.error else s.status.state}")
    return s.result.data_array or []


def main() -> int:
    w = WorkspaceClient(host=WORKSPACE_HOST, auth_type="azure-cli")
    wh = next(iter(w.warehouses.list()))
    print(f"Warehouse: {wh.name} ({wh.state.value})")

    entities = [
        ("order_lines", "velora_oms"),
        ("products", "velora_pim"),
        ("product_pricing", "velora_pim"),
        ("sales_reps", "velora_hrm"),
        ("territory_assignments", "velora_hrm"),
    ]

    for entity, _src in entities:
        print(f"\n=== {entity} ===")
        for label, sql in [
            ("bronze count",   f"SELECT COUNT(*) FROM bronze.default.{entity}"),
            ("silver count",   f"SELECT COUNT(*) FROM silver.default.{entity}"),
            ("dq_passed=false (should be 0)", f"SELECT COUNT(*) FROM silver.default.{entity} WHERE dq_passed = false"),
            ("quarantine count", f"SELECT COUNT(*) FROM quarantine.default.{entity}"),
        ]:
            try:
                rows = run_sql(w, wh.id, sql)
                val = rows[0][0] if rows else "?"
                print(f"  {label:35s} {val}")
            except Exception as e:
                msg = str(e).split("\n")[0][:120]
                print(f"  {label:35s} ERROR: {msg}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
