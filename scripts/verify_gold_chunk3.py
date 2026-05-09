"""End-to-end verification of S11 chunk 3: fact_order_line + fact_daily_channel_revenue.

Validates row counts, FK invariants, as-of-join coverage, channel-derived
territory rules, measure consistency, and the rollup reconciles against
the fact-line. Auth: az login session.
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
        # ── fact_order_line ─────────────────────────────────────────────
        ("fact_order_line total rows (expect 12,300 = silver.order_lines)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line"),
        ("fact_order_line distinct line_id (must == total)",
         "SELECT COUNT(DISTINCT line_id) FROM gold.default.fact_order_line"),
        ("silver.order_lines clean rows (target for fact)",
         "SELECT COUNT(*) FROM silver.default.order_lines WHERE dq_passed=true"),

        # ── as-of coverage ──────────────────────────────────────────────
        ("rows missing customer_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE customer_surrogate_key IS NULL"),
        ("rows missing product_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE product_surrogate_key IS NULL"),
        ("rows missing territory_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE territory_id IS NULL"),

        # ── channel-conditional invariants ──────────────────────────────
        ("D2C rows with non-null store_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='D2C' AND store_id IS NOT NULL"),
        ("D2C rows with non-null rep_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='D2C' AND rep_surrogate_key IS NOT NULL"),
        ("D2C rows where territory_id != 'D2C_NATIONAL' (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='D2C' AND territory_id<>'D2C_NATIONAL'"),
        ("STORE rows with null store_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='STORE' AND store_id IS NULL"),
        ("STORE rows with non-null rep_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='STORE' AND rep_surrogate_key IS NOT NULL"),
        ("B2B rows with null rep_surrogate_key (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='B2B' AND rep_surrogate_key IS NULL"),
        ("B2B rows with non-null store_id (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE channel_id='B2B' AND store_id IS NOT NULL"),

        # ── FK validity ─────────────────────────────────────────────────
        ("FK chk: fact.customer_surrogate_key NOT in dim_customer (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_customer dc ON f.customer_surrogate_key=dc.surrogate_key "
         "WHERE dc.surrogate_key IS NULL"),
        ("FK chk: fact.product_surrogate_key NOT in dim_product (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_product dp ON f.product_surrogate_key=dp.surrogate_key "
         "WHERE dp.surrogate_key IS NULL"),
        ("FK chk: fact.rep_surrogate_key NOT in dim_sales_rep (excluding NULL) (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_sales_rep dr ON f.rep_surrogate_key=dr.surrogate_key "
         "WHERE f.rep_surrogate_key IS NOT NULL AND dr.surrogate_key IS NULL"),
        ("FK chk: fact.territory_id NOT in dim_territory (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_territory dt ON f.territory_id=dt.territory_id "
         "WHERE dt.territory_id IS NULL"),
        ("FK chk: fact.order_date_id NOT in dim_date (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_date dd ON f.order_date_id=dd.date_id "
         "WHERE dd.date_id IS NULL"),
        ("FK chk: fact.channel_id NOT in dim_sales_channel (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_sales_channel dsc ON f.channel_id=dsc.channel_id "
         "WHERE dsc.channel_id IS NULL"),
        ("FK chk: fact.status_id NOT in dim_order_status (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_order_status dos ON f.status_id=dos.status_id "
         "WHERE dos.status_id IS NULL"),

        # ── measure sanity ──────────────────────────────────────────────
        ("rows where tax_amount != round(line_total_inr * 0.18, 2) (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line "
         "WHERE tax_amount <> round(line_total_inr * 0.18, 2)"),
        ("rows where net_revenue_inr != line_total_inr (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE net_revenue_inr <> line_total_inr"),
        ("rows with negative or zero quantity (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line WHERE quantity_ordered <= 0"),
        ("channel breakdown",
         "SELECT channel_id, COUNT(*) c, ROUND(SUM(net_revenue_inr),2) net_rev "
         "FROM gold.default.fact_order_line GROUP BY channel_id ORDER BY c DESC"),

        # ── fact_daily_channel_revenue ──────────────────────────────────
        ("rollup total rows",
         "SELECT COUNT(*) FROM gold.default.fact_daily_channel_revenue"),
        ("rollup distinct grain (must == total)",
         "SELECT COUNT(*) FROM ("
         "  SELECT summary_date_id, channel_id, category_id, territory_id "
         "  FROM gold.default.fact_daily_channel_revenue "
         "  GROUP BY 1,2,3,4)"),
        ("rollup units = fact units (recon)",
         "SELECT "
         " (SELECT SUM(quantity_ordered) FROM gold.default.fact_order_line) AS fact, "
         " (SELECT SUM(total_units_sold) FROM gold.default.fact_daily_channel_revenue) AS rollup"),
        ("rollup net_revenue = fact net_revenue (recon)",
         "SELECT "
         " ROUND((SELECT SUM(net_revenue_inr) FROM gold.default.fact_order_line),2) AS fact, "
         " ROUND((SELECT SUM(net_revenue_inr) FROM gold.default.fact_daily_channel_revenue),2) AS rollup"),
        ("rollup top 5 (date,channel,category,territory) by net_revenue",
         "SELECT summary_date_id, channel_id, category_id, territory_id, net_revenue_inr "
         "FROM gold.default.fact_daily_channel_revenue ORDER BY net_revenue_inr DESC LIMIT 5"),
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
