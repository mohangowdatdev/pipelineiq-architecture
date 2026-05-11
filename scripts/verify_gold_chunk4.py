"""End-to-end verification of S11.2 chunk 4: silver.inventory_snapshot,
silver.order_status_log, silver.customer_addresses, gold.fact_inventory_daily.

Validates row counts, FK invariants, derived measures (days_of_stock_remaining
NULL rules), and reconciliation against silver/source. Auth: az login session.
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
    print(f"Warehouse: {wh.name} ({wh.state.value})\n")

    queries = [
        # ── silver.inventory_snapshot ───────────────────────────────────
        ("silver.inventory_snapshot total rows",
         "SELECT COUNT(*) FROM silver.default.inventory_snapshot"),
        ("silver.inventory_snapshot rows with dq_passed=false",
         "SELECT COUNT(*) FROM silver.default.inventory_snapshot WHERE dq_passed=false"),
        ("silver.inventory_snapshot per-snapshot_date row counts (each = 189,225 expected)",
         "SELECT snapshot_date, COUNT(*) AS n "
         "FROM silver.default.inventory_snapshot GROUP BY snapshot_date ORDER BY snapshot_date"),
        ("silver.inventory_snapshot composite-key uniqueness "
         "(distinct (product_id, store_id, snapshot_date) MUST == total)",
         "SELECT "
         " (SELECT COUNT(*) FROM silver.default.inventory_snapshot) AS total, "
         " (SELECT COUNT(*) FROM (SELECT product_id, store_id, snapshot_date "
         "                        FROM silver.default.inventory_snapshot "
         "                        GROUP BY 1,2,3)) AS distinct_grain"),

        # ── silver.order_status_log ─────────────────────────────────────
        ("silver.order_status_log total rows",
         "SELECT COUNT(*) FROM silver.default.order_status_log"),
        ("silver.order_status_log per-status transition counts (top 10)",
         "SELECT from_status, to_status, COUNT(*) c "
         "FROM silver.default.order_status_log "
         "GROUP BY from_status, to_status ORDER BY c DESC LIMIT 10"),
        ("silver.order_status_log dq_passed=false rows",
         "SELECT COUNT(*) FROM silver.default.order_status_log WHERE dq_passed=false"),

        # ── silver.customer_addresses ───────────────────────────────────
        ("silver.customer_addresses total rows",
         "SELECT COUNT(*) FROM silver.default.customer_addresses"),
        ("silver.customer_addresses primary count per customer "
         "(every customer should have exactly 1 primary)",
         "SELECT n_primary, COUNT(*) AS n_customers "
         "FROM (SELECT customer_id, SUM(CAST(is_primary AS INT)) AS n_primary "
         "      FROM silver.default.customer_addresses GROUP BY customer_id) "
         "GROUP BY n_primary ORDER BY n_primary"),
        ("silver.customer_addresses dq_passed=false rows",
         "SELECT COUNT(*) FROM silver.default.customer_addresses WHERE dq_passed=false"),

        # ── gold.fact_inventory_daily ───────────────────────────────────
        ("fact_inventory_daily total rows (expect = silver.inventory_snapshot DQ-passed)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily"),
        ("fact_inventory_daily distinct grain (must == total)",
         "SELECT "
         " (SELECT COUNT(*) FROM gold.default.fact_inventory_daily) AS total, "
         " (SELECT COUNT(*) FROM (SELECT snapshot_date_id, product_surrogate_key, store_id "
         "                        FROM gold.default.fact_inventory_daily GROUP BY 1,2,3)) AS distinct_grain"),

        # ── as-of coverage on fact_inventory_daily ──────────────────────
        ("fact_inventory_daily missing product_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily WHERE product_surrogate_key IS NULL"),
        ("fact_inventory_daily missing store_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily WHERE store_id IS NULL"),
        ("fact_inventory_daily missing snapshot_date_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily WHERE snapshot_date_id IS NULL"),

        # ── FK validity ─────────────────────────────────────────────────
        ("FK chk: fact.product_surrogate_key NOT in dim_product (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily f "
         "LEFT JOIN gold.default.dim_product dp ON f.product_surrogate_key=dp.surrogate_key "
         "WHERE dp.surrogate_key IS NULL"),
        ("FK chk: fact.store_id NOT in dim_store (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily f "
         "LEFT JOIN gold.default.dim_store ds ON f.store_id=ds.store_id "
         "WHERE ds.store_id IS NULL"),
        ("FK chk: fact.snapshot_date_id NOT in dim_date (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily f "
         "LEFT JOIN gold.default.dim_date dd ON f.snapshot_date_id=dd.date_id "
         "WHERE dd.date_id IS NULL"),

        # ── derived-measure sanity (days_of_stock_remaining) ────────────
        ("rows w/ days_of_stock_remaining IS NULL (early dates + zero-sales pairs)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily "
         "WHERE days_of_stock_remaining IS NULL"),
        ("rows w/ days_of_stock_remaining < 0 (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily "
         "WHERE days_of_stock_remaining < 0"),
        ("days_of_stock NULL by snapshot_date_id "
         "(early days should be ALL NULL since <7d history; later dates non-NULL where avg sales > 0)",
         "SELECT snapshot_date_id, "
         " SUM(CASE WHEN days_of_stock_remaining IS NULL THEN 1 ELSE 0 END) AS n_null, "
         " SUM(CASE WHEN days_of_stock_remaining IS NOT NULL THEN 1 ELSE 0 END) AS n_not_null, "
         " COUNT(*) AS n_total "
         "FROM gold.default.fact_inventory_daily "
         "GROUP BY snapshot_date_id ORDER BY snapshot_date_id"),

        # ── reconciliation against silver ───────────────────────────────
        ("RECON: SUM(closing_stock) silver vs gold (must match exactly)",
         "SELECT "
         " (SELECT SUM(closing_stock) FROM silver.default.inventory_snapshot WHERE dq_passed=true) AS silver, "
         " (SELECT SUM(closing_stock) FROM gold.default.fact_inventory_daily) AS gold"),
        ("RECON: SUM(units_sold) silver vs gold (must match exactly)",
         "SELECT "
         " (SELECT SUM(units_sold) FROM silver.default.inventory_snapshot WHERE dq_passed=true) AS silver, "
         " (SELECT SUM(units_sold) FROM gold.default.fact_inventory_daily) AS gold"),
        ("stockout flag rate by snapshot_date_id",
         "SELECT snapshot_date_id, "
         " ROUND(100.0 * SUM(CASE WHEN stockout_flag THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_stockout "
         "FROM gold.default.fact_inventory_daily GROUP BY snapshot_date_id ORDER BY snapshot_date_id"),
    ]

    for label, sql in queries:
        print(f"--- {label} ---")
        try:
            for r in run_sql(w, wh.id, sql):
                print("  " + " | ".join(str(c) for c in r))
        except Exception as e:
            print(f"  ERROR: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
