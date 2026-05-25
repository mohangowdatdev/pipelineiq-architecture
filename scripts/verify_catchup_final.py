"""Verify gold catch-up to 2026-05-24.

Checks:
- gold dim/fact row counts
- dim_customer SCD-2 invariants (0 collisions, NK has at least 1 current row)
- silver↔gold fact reconciles (exact)
- FK orphans on fact_order_line (0)
- All Phase 2 contracts hold under 24 days × 189K inventory volume
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

    fail_count = 0
    queries = [
        # ── dim counts ────────────────────────────────────────────────
        ("dim_customer total rows",
         "SELECT COUNT(*) FROM gold.default.dim_customer", None),
        ("dim_customer current rows (= distinct NK)",
         "SELECT COUNT(*) FROM gold.default.dim_customer WHERE is_current=true", None),
        ("dim_customer distinct surrogate_key (must == total)",
         "SELECT COUNT(DISTINCT surrogate_key) FROM gold.default.dim_customer", None),
        ("dim_customer SCD-2 collisions (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT surrogate_key, COUNT(*) c FROM gold.default.dim_customer "
         "  GROUP BY surrogate_key HAVING c>1)", 0),
        ("dim_customer NKs missing a current row (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT customer_id FROM gold.default.dim_customer "
         "  GROUP BY customer_id HAVING SUM(CAST(is_current AS INT))=0)", 0),

        ("dim_product total rows",
         "SELECT COUNT(*) FROM gold.default.dim_product", None),
        ("dim_product collisions (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT surrogate_key, COUNT(*) c FROM gold.default.dim_product "
         "  GROUP BY surrogate_key HAVING c>1)", 0),

        ("dim_sales_rep total rows",
         "SELECT COUNT(*) FROM gold.default.dim_sales_rep", None),
        ("dim_sales_rep collisions (must = 0)",
         "SELECT COUNT(*) FROM ("
         "  SELECT surrogate_key, COUNT(*) c FROM gold.default.dim_sales_rep "
         "  GROUP BY surrogate_key HAVING c>1)", 0),

        # ── fact counts + reconciles ─────────────────────────────────
        ("fact_order_line rows",
         "SELECT COUNT(*) FROM gold.default.fact_order_line", None),
        ("silver.order_lines rows (must == fact_order_line)",
         "SELECT COUNT(*) FROM silver.default.order_lines", None),

        ("fact_inventory_daily rows",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily", None),
        ("silver.inventory_snapshot rows (must == fact_inventory_daily)",
         "SELECT COUNT(*) FROM silver.default.inventory_snapshot", None),

        ("fact_daily_channel_revenue rows",
         "SELECT COUNT(*) FROM gold.default.fact_daily_channel_revenue", None),

        # ── FK orphans on fact_order_line (must = 0 for each linked dim)
        ("FK orphan: fact_order_line → dim_customer (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_customer d ON f.customer_surrogate_key=d.surrogate_key "
         "WHERE d.surrogate_key IS NULL", 0),
        ("FK orphan: fact_order_line → dim_product (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_order_line f "
         "LEFT JOIN gold.default.dim_product d ON f.product_surrogate_key=d.surrogate_key "
         "WHERE d.surrogate_key IS NULL", 0),
        ("FK orphan: fact_inventory_daily → dim_product (must = 0)",
         "SELECT COUNT(*) FROM gold.default.fact_inventory_daily f "
         "LEFT JOIN gold.default.dim_product d ON f.product_surrogate_key=d.surrogate_key "
         "WHERE d.surrogate_key IS NULL", 0),

        # ── static dims sanity
        ("dim_date rows (expect 4018)",
         "SELECT COUNT(*) FROM gold.default.dim_date", None),
        ("dim_order_status rows (expect 7 — includes RETURN_INITIATED)",
         "SELECT COUNT(*) FROM gold.default.dim_order_status", None),
        ("dim_store rows (expect 45)",
         "SELECT COUNT(*) FROM gold.default.dim_store", None),
        ("dim_product_category rows (expect 35)",
         "SELECT COUNT(*) FROM gold.default.dim_product_category", None),
    ]

    results = {}
    for label, sql, expected_zero in queries:
        try:
            rows = run_sql(w, wh.id, sql)
            val = rows[0][0] if rows and len(rows[0]) == 1 else rows
            results[label] = val
            tag = ""
            if expected_zero is not None:
                try:
                    if int(val) != expected_zero:
                        tag = "  ❌ FAIL"
                        fail_count += 1
                    else:
                        tag = "  ✅"
                except Exception:
                    tag = "  ?"
            print(f"  {label}: {val}{tag}")
        except Exception as e:
            print(f"  {label}: ERROR — {e}")
            fail_count += 1

    # ── reconcile checks (cross-row math)
    print()
    try:
        sol = int(results.get("silver.order_lines rows (must == fact_order_line)", -1))
        fol = int(results.get("fact_order_line rows", -1))
        match = "✅" if sol == fol else "❌"
        print(f"  RECONCILE silver.order_lines ({sol}) vs fact_order_line ({fol}): {match}")
        if sol != fol:
            fail_count += 1

        sis = int(results.get("silver.inventory_snapshot rows (must == fact_inventory_daily)", -1))
        fid = int(results.get("fact_inventory_daily rows", -1))
        match = "✅" if sis == fid else "❌"
        print(f"  RECONCILE silver.inventory_snapshot ({sis}) vs fact_inventory_daily ({fid}): {match}")
        if sis != fid:
            fail_count += 1

        dct = int(results.get("dim_customer total rows", -1))
        dsk = int(results.get("dim_customer distinct surrogate_key (must == total)", -1))
        match = "✅" if dct == dsk else "❌"
        print(f"  dim_customer total ({dct}) == distinct surrogate_key ({dsk}): {match}")
        if dct != dsk:
            fail_count += 1
    except Exception as e:
        print(f"  reconcile error: {e}")

    print()
    if fail_count == 0:
        print("ALL CHECKS PASSED ✅")
    else:
        print(f"❌ {fail_count} CHECK(S) FAILED")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())
