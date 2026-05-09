"""Quick verification of the 3 SCD-2 + synthesized dims built in S10 chunk 2.

Checks row counts, key sanity, SCD-2 invariants, sentinel presence,
and fact-join readiness against silver. Auth: az login session.
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
        # ── dim_product ──────────────────────────────────────────────
        ("dim_product total rows",
         "SELECT COUNT(*) FROM gold.default.dim_product"),
        ("dim_product current rows (= distinct product_id)",
         "SELECT COUNT(*) FROM gold.default.dim_product WHERE is_current=true"),
        ("dim_product distinct product_id",
         "SELECT COUNT(DISTINCT product_id) FROM gold.default.dim_product"),
        ("dim_product distinct surrogate_key (must == total)",
         "SELECT COUNT(DISTINCT surrogate_key) FROM gold.default.dim_product"),
        ("dim_product non-current versions (price changes)",
         "SELECT COUNT(*) FROM gold.default.dim_product WHERE is_current=false"),
        ("dim_product (NK,valid_from) duplicate count (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT product_id, valid_from, COUNT(*) c FROM gold.default.dim_product"
         "  GROUP BY product_id, valid_from HAVING c>1)"),
        ("dim_product valid_to < valid_from violations (must = 0)",
         "SELECT COUNT(*) FROM gold.default.dim_product "
         "WHERE valid_to IS NOT NULL AND valid_to < valid_from"),
        ("dim_product silver.product_pricing row count",
         "SELECT COUNT(*) FROM silver.default.product_pricing WHERE dq_passed=true"),

        # ── dim_sales_rep ────────────────────────────────────────────
        ("dim_sales_rep total rows",
         "SELECT COUNT(*) FROM gold.default.dim_sales_rep"),
        ("dim_sales_rep current rows (= distinct rep_id)",
         "SELECT COUNT(*) FROM gold.default.dim_sales_rep WHERE is_current=true"),
        ("dim_sales_rep distinct rep_id",
         "SELECT COUNT(DISTINCT rep_id) FROM gold.default.dim_sales_rep"),
        ("dim_sales_rep distinct surrogate_key (must == total)",
         "SELECT COUNT(DISTINCT surrogate_key) FROM gold.default.dim_sales_rep"),
        ("dim_sales_rep (NK,valid_from) duplicate count (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT rep_id, valid_from, COUNT(*) c FROM gold.default.dim_sales_rep"
         "  GROUP BY rep_id, valid_from HAVING c>1)"),
        ("dim_sales_rep valid_to < valid_from violations (must = 0)",
         "SELECT COUNT(*) FROM gold.default.dim_sales_rep "
         "WHERE valid_to IS NOT NULL AND valid_to < valid_from"),
        ("dim_sales_rep territory breakdown (current)",
         "SELECT territory_id, COUNT(*) FROM gold.default.dim_sales_rep "
         "WHERE is_current=true GROUP BY territory_id ORDER BY territory_id"),

        # ── dim_territory ────────────────────────────────────────────
        ("dim_territory total rows",
         "SELECT COUNT(*) FROM gold.default.dim_territory"),
        ("dim_territory distinct territory_id (must == total)",
         "SELECT COUNT(DISTINCT territory_id) FROM gold.default.dim_territory"),
        ("dim_territory D2C_NATIONAL sentinel present (must = 1)",
         "SELECT COUNT(*) FROM gold.default.dim_territory WHERE territory_id='D2C_NATIONAL'"),
        ("dim_territory region breakdown",
         "SELECT region, COUNT(*) FROM gold.default.dim_territory GROUP BY region ORDER BY 2 DESC"),
        ("dim_territory rows with NULL city (only D2C_NATIONAL expected)",
         "SELECT territory_id FROM gold.default.dim_territory WHERE city IS NULL"),

        # ── fact-join readiness ──────────────────────────────────────
        ("FK chk: dim_store.territory_id values not in dim_territory (must = 0)",
         "SELECT COUNT(DISTINCT s.territory_id) FROM gold.default.dim_store s "
         "LEFT JOIN gold.default.dim_territory t ON s.territory_id=t.territory_id "
         "WHERE t.territory_id IS NULL AND s.territory_id IS NOT NULL"),
        ("FK chk: dim_sales_rep.territory_id values not in dim_territory (must = 0)",
         "SELECT COUNT(DISTINCT r.territory_id) FROM gold.default.dim_sales_rep r "
         "LEFT JOIN gold.default.dim_territory t ON r.territory_id=t.territory_id "
         "WHERE t.territory_id IS NULL AND r.territory_id IS NOT NULL"),
        ("FK chk: dim_product.category_id values not in dim_product_category (must = 0)",
         "SELECT COUNT(DISTINCT p.category_id) FROM gold.default.dim_product p "
         "LEFT JOIN gold.default.dim_product_category c ON p.category_id=c.category_id "
         "WHERE c.category_id IS NULL AND p.category_id IS NOT NULL"),
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
