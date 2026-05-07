"""Quick verification of silver.default.orders + quarantine.default.orders.

Runs a few SQL queries via the SQL warehouse to confirm:
- Total row counts.
- DQ split (passed vs. quarantined).
- Sample rejection reasons.

Auth: az login session via Databricks SDK.
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

    queries = [
        ("bronze.default.orders count",            "SELECT COUNT(*) FROM bronze.default.orders"),
        ("bronze.default.orders distinct order_id", "SELECT COUNT(DISTINCT order_id) FROM bronze.default.orders"),
        ("silver.default.orders count",            "SELECT COUNT(*) FROM silver.default.orders"),
        ("silver passed=true",                     "SELECT COUNT(*) FROM silver.default.orders WHERE dq_passed = true"),
        ("silver passed=false (should be 0)",      "SELECT COUNT(*) FROM silver.default.orders WHERE dq_passed = false"),
        ("quarantine.default.orders count",        "SELECT COUNT(*) FROM quarantine.default.orders"),
        ("quarantine reasons",                     "SELECT rejection_reason, COUNT(*) FROM quarantine.default.orders GROUP BY rejection_reason ORDER BY 2 DESC"),
        ("silver channel split",                   "SELECT channel_type, COUNT(*) FROM silver.default.orders GROUP BY channel_type ORDER BY 2 DESC"),
        ("silver date range",                      "SELECT MIN(order_date), MAX(order_date) FROM silver.default.orders"),
        ("silver schema sanity",                   "DESCRIBE silver.default.orders"),
    ]

    for label, sql in queries:
        print(f"\n--- {label} ---")
        try:
            rows = run_sql(w, wh.id, sql)
            for r in rows:
                print("  " + " | ".join(str(c) for c in r))
        except Exception as e:
            print(f"  ERROR: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
