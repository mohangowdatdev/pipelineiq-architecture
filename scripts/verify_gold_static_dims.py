"""Quick verification of the 5 static dims built in S10 chunk 1.

Checks row counts + key sanity. Auth: az login session.
"""

from __future__ import annotations

import sys
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

WORKSPACE_HOST = "https://adb-7405617631498102.2.azuredatabricks.net"


def run_sql(w, warehouse_id, sql):
    s = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=sql, wait_timeout="30s"
    )
    while s.status.state in (StatementState.PENDING, StatementState.RUNNING):
        s = w.statement_execution.get_statement(s.statement_id)
    if s.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(s.status.error.message if s.status.error else s.status.state)
    return s.result.data_array or []


def main() -> int:
    w = WorkspaceClient(host=WORKSPACE_HOST, auth_type="azure-cli")
    wh = next(iter(w.warehouses.list()))
    print(f"Warehouse: {wh.name} ({wh.state.value})")

    queries = [
        ("dim_date count",                          "SELECT COUNT(*) FROM gold.default.dim_date"),
        ("dim_date date range",                     "SELECT MIN(full_date), MAX(full_date) FROM gold.default.dim_date"),
        ("dim_date holidays in 2026",               "SELECT COUNT(*) FROM gold.default.dim_date WHERE year=2026 AND is_public_holiday=true"),
        ("dim_date FY26 days (should be 365)",      "SELECT COUNT(*) FROM gold.default.dim_date WHERE fiscal_year=2026"),
        ("dim_sales_channel count (expect 3)",      "SELECT COUNT(*) FROM gold.default.dim_sales_channel"),
        ("dim_order_status count (expect 6)",       "SELECT COUNT(*) FROM gold.default.dim_order_status"),
        ("dim_product_category count (expect 35)",  "SELECT COUNT(*) FROM gold.default.dim_product_category"),
        ("dim_store count (expect 45)",             "SELECT COUNT(*) FROM gold.default.dim_store"),
        ("dim_store distinct territories",          "SELECT COUNT(DISTINCT territory_id) FROM gold.default.dim_store"),
        ("dim_store tier breakdown",                "SELECT store_tier, COUNT(*) FROM gold.default.dim_store GROUP BY store_tier ORDER BY 2 DESC"),
    ]

    for label, sql in queries:
        print(f"\n--- {label} ---")
        try:
            for r in run_sql(w, wh.id, sql):
                print("  " + " | ".join(str(c) for c in r))
        except Exception as e:
            print(f"  ERROR: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
