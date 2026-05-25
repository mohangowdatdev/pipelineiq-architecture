"""Verify bronze + silver catch-up to 2026-05-24 BEFORE gold completes.

Confirms that the bronze/silver multi-task Job successes correspond to
actual row growth — not just notebook exit code 0. Run while gold is in
flight so we either trust the green or catch a silent regression early.
"""
from __future__ import annotations

import sys
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

WORKSPACE_HOST = "https://adb-7405617631498102.2.azuredatabricks.net"


def run_sql(w, warehouse_id, sql):
    s = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=sql, wait_timeout="50s"
    )
    while s.status.state in (StatementState.PENDING, StatementState.RUNNING):
        s = w.statement_execution.get_statement(s.statement_id)
    if s.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(s.status.error.message if s.status.error else s.status.state)
    return s.result.data_array or []


def main() -> int:
    w = WorkspaceClient(host=WORKSPACE_HOST, auth_type="azure-cli")
    wh = next(iter(w.warehouses.list()))
    print(f"Warehouse: {wh.name} ({wh.state.value})\n")

    queries = [
        # ── source coverage (sanity — should hit velora_oms.* directly if there were a JDBC catalog,
        #     but here we use silver as the proxy for "source completeness")
        ("silver.orders max order_date",
         "SELECT MAX(order_date) FROM silver.default.orders"),
        ("silver.orders rows",
         "SELECT COUNT(*) FROM silver.default.orders"),
        ("silver.orders by date (last 14 days)",
         "SELECT order_date, COUNT(*) FROM silver.default.orders "
         "WHERE order_date >= '2026-05-11' GROUP BY order_date ORDER BY order_date"),

        # ── inventory: the heavy lifter
        ("silver.inventory_snapshot total rows (expect ~4.54M)",
         "SELECT COUNT(*) FROM silver.default.inventory_snapshot"),
        ("silver.inventory_snapshot distinct snapshot_dates",
         "SELECT COUNT(DISTINCT snapshot_date) FROM silver.default.inventory_snapshot"),
        ("silver.inventory_snapshot last 14 days",
         "SELECT snapshot_date, COUNT(*) FROM silver.default.inventory_snapshot "
         "WHERE snapshot_date >= '2026-05-11' GROUP BY snapshot_date ORDER BY snapshot_date"),

        # ── order_lines (silver dedups bronze)
        ("silver.order_lines rows",
         "SELECT COUNT(*) FROM silver.default.order_lines"),

        # ── order_status_log
        ("silver.order_status_log rows",
         "SELECT COUNT(*) FROM silver.default.order_status_log"),

        # ── customers + addresses
        ("silver.customers rows",
         "SELECT COUNT(*) FROM silver.default.customers"),
        ("silver.customer_addresses rows",
         "SELECT COUNT(*) FROM silver.default.customer_addresses"),

        # ── DQ rejects (should be 0)
        ("quarantine.orders rows (expect 0)",
         "SELECT COUNT(*) FROM quarantine.default.orders"),
        ("quarantine.order_lines rows (expect 0)",
         "SELECT COUNT(*) FROM quarantine.default.order_lines"),

        # ── bronze sanity (append-only — silver dedups)
        ("bronze.inventory_snapshot rows (>= silver because no dedup)",
         "SELECT COUNT(*) FROM bronze.default.inventory_snapshot"),
        ("bronze.orders rows",
         "SELECT COUNT(*) FROM bronze.default.orders"),
    ]

    for label, sql in queries:
        try:
            rows = run_sql(w, wh.id, sql)
            if len(rows) == 1 and len(rows[0]) == 1:
                print(f"  {label}: {rows[0][0]}")
            else:
                print(f"  {label}:")
                for r in rows:
                    print(f"    {r}")
        except Exception as e:
            print(f"  {label}: ERROR — {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
