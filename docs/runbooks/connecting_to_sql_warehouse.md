# Connecting to the Databricks SQL Warehouse

The `pipelineiq-dev-sqlwh` warehouse is the SQL endpoint for all 4 Unity
Catalog catalogs (`bronze`, `silver`, `gold`, `quarantine`). Notebooks
are one consumer; this runbook covers the others.

## Connection facts

| Field | Value |
|---|---|
| Workspace host | `https://adb-7405617631498102.2.azuredatabricks.net` |
| HTTP path | `/sql/1.0/warehouses/71a1e581f197abf0` |
| Warehouse name | `pipelineiq-dev-sqlwh` |
| Warehouse ID | `71a1e581f197abf0` |
| JDBC URL | `jdbc:spark://adb-7405617631498102.2.azuredatabricks.net:443/default;transportMode=http;ssl=1;AuthMech=3;httpPath=/sql/1.0/warehouses/71a1e581f197abf0;` |
| Auto-stop | 10 min idle (warehouse is `STOPPED` until first query auto-starts it) |
| Cold-start | ~30-60s on first query after stop |

## Auth

OAuth User-to-Machine (browser pop-up) is the default for interactive
clients. Uses your `mohan.gowda@SailAnalyticsAP.onmicrosoft.com` SSO.

For headless / CI use, generate a Personal Access Token in the workspace:
**User Settings → Developer → Access tokens → Generate new token**. Pass
as the `password` field with `username` = `token`. Don't commit PATs.

## Setup option A: Databricks VS Code extension (recommended)

Best for: SQL + notebooks side-by-side, structured browsing of catalogs.

1. VS Code → Extensions → install **Databricks** (publisher: Databricks)
2. ⌘⇧P → `Databricks: Configure Workspace`
3. Host: paste the workspace URL above
4. Auth: choose **OAuth (User to Machine)** → browser sign-in
5. Sidebar → **Compute** → **SQL Warehouses** → click `pipelineiq-dev-sqlwh`
   to set active
6. Create a `.sql` file → right-click → **Run File on Databricks**

## Setup option B: SQLTools + Databricks driver

Best for: "feels like a regular SQL Server" — connection list, query
files, multi-tab results.

1. Install **SQLTools** (Matheus Teixeira) + **SQLTools Databricks
   Driver** (Databricks)
2. SQLTools sidebar → Add New Connection → Databricks
3. Fill:
   - Server hostname: `adb-7405617631498102.2.azuredatabricks.net`
   - HTTP path: `/sql/1.0/warehouses/71a1e581f197abf0`
   - Auth type: OAuth (U2M)
   - Catalog: `gold` (override per query with `USE CATALOG ...`)
   - Schema: `default`
4. Test → Save → Run queries with ⌘E ⌘E

## Setup option C: DBeaver or any JDBC client

1. Download Databricks JDBC driver:
   https://www.databricks.com/spark/jdbc-drivers-download
2. New connection → use the JDBC URL from the table above
3. Auth: choose OAuth or paste a PAT
4. Test → save

## Setup option D: Power BI / Tableau

Both have native Databricks connectors. Use the workspace host + HTTP path
from above; auth is OAuth.

## Setup option E: Python (already used by all `scripts/verify_*.py`)

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient(
    host="https://adb-7405617631498102.2.azuredatabricks.net",
    auth_type="azure-cli",  # or pass token=
)
wh = next(iter(w.warehouses.list()))
result = w.statement_execution.execute_statement(
    warehouse_id=wh.id,
    statement="SELECT COUNT(*) FROM gold.default.dim_customer",
    wait_timeout="30s",
)
```

## Smoke-test query (copy-paste)

Once connected, run this — exercises gold + a 3-way join:

```sql
SELECT dpc.category_name,
       COUNT(DISTINCT dp.product_id) AS skus,
       SUM(f.units_sold)             AS units_sold_5_13,
       AVG(f.closing_stock)          AS avg_stock_5_13
FROM gold.default.fact_inventory_daily f
JOIN gold.default.dim_product          dp  ON f.product_surrogate_key = dp.surrogate_key
JOIN gold.default.dim_product_category dpc USING (category_id)
WHERE f.snapshot_date = '2026-05-13'
GROUP BY dpc.category_name
ORDER BY units_sold_5_13 DESC;
```

Should return 35 category rows. If you see "warehouse starting", that's
the cold-start — wait ~30-60s and re-run.

## Common gotchas

- **OAuth fails in extension** → try `az logout && az login` to refresh
  the underlying SSO. The extension piggybacks on it for some flows.
- **"Warehouse not running"** → first query auto-starts it. Subsequent
  queries are instant until the 10-min auto-stop kicks in.
- **Tables not visible** → make sure you're on the `mohangowda` user, not
  a different account. Unity Catalog grants are per-user.
- **`USE CATALOG bronze` works in SQL but the connection panel shows gold**
  — that's a SQLTools quirk; the catalog you set in the connection is
  the default, not a hard limit. Cross-catalog queries work fine.

## Cost note

The warehouse runs at 2X-Small Classic (cheapest tier) with auto-stop
at 10 min. Idle time = no cost. A typical ad-hoc session is a few cents.
Power BI dashboards refreshing on a schedule cost more — review before
enabling scheduled refresh.
