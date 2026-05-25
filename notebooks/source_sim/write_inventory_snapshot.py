# Databricks notebook source
# MAGIC %md
# MAGIC # Source-system simulator — daily inventory snapshot
# MAGIC
# MAGIC Synthesizes 189,225 (`product_id` × `store_id`) rows and writes them to
# MAGIC `velora_pim.inventory_snapshot` for a given `snapshot_date`. This notebook
# MAGIC **simulates Velora's nightly warehouse snapshot job** — it is part of the
# MAGIC source-system layer, not the medallion ETL. It owes its existence to the
# MAGIC Flex Consumption Function App being too small/fragile to push 189K rows
# MAGIC through pyodbc reliably (DECISIONS #62 / #69 history; superseded by the
# MAGIC migration this notebook embodies).
# MAGIC
# MAGIC ## Layer boundary
# MAGIC
# MAGIC **This is the source side, not ETL.** `velora_oms` / `velora_pim` are the
# MAGIC system-of-record from PipelineIQ's perspective. The medallion (bronze /
# MAGIC silver / gold) is downstream and never appears in this notebook.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required | default |
# MAGIC |---|---|---|---|
# MAGIC | `snapshot_date` | `2026-05-25` or `yesterday_utc` | yes | `yesterday_utc` |
# MAGIC | `force` | `true` / `false` | no | `false` — refuse if rows already exist |
# MAGIC | `verify_order_landed` | `true` / `false` | no | `true` — refuse if no orders for date |
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC `force=true` does `DELETE WHERE snapshot_date=?` first, then INSERT. Safe
# MAGIC to re-run for the same date without duplicating rows. PK is `snapshot_id`
# MAGIC (UUID); natural key is `(product_id, store_id, snapshot_date)`.
# MAGIC
# MAGIC ## Two-writer race protection
# MAGIC
# MAGIC The Function App writes orders/lines/status_log at 00:30 UTC. This
# MAGIC notebook fires at 00:35 UTC. `verify_order_landed=true` (default) makes
# MAGIC the notebook refuse to write if `velora_oms.orders` has 0 rows for the
# MAGIC target date — proves the Function fired first.

# COMMAND ----------

import datetime as _dt
from pyspark.sql import functions as F

# COMMAND ----------

dbutils.widgets.text("snapshot_date", "yesterday_utc")
dbutils.widgets.text("force", "false")
dbutils.widgets.text("verify_order_landed", "true")

raw_date = dbutils.widgets.get("snapshot_date").strip()
force = dbutils.widgets.get("force").strip().lower() == "true"
verify_order_landed = dbutils.widgets.get("verify_order_landed").strip().lower() == "true"

if raw_date == "yesterday_utc" or raw_date == "":
    snapshot_date = _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)
else:
    snapshot_date = _dt.date.fromisoformat(raw_date)

print(f"snapshot_date       = {snapshot_date}")
print(f"force               = {force}")
print(f"verify_order_landed = {verify_order_landed}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## JDBC connection

# COMMAND ----------

SECRET_SCOPE = "pipelineiq-dev-kv"
SQL_SERVER = "pipelineiq-sql-velora-dev.database.windows.net"
SQL_DB = "velora_oms"
SQL_USER = "pipelineiqadmin"
SQL_PASSWORD = dbutils.secrets.get(scope=SECRET_SCOPE, key="sql-admin-password")

JDBC_URL = (
    f"jdbc:sqlserver://{SQL_SERVER}:1433;database={SQL_DB};"
    "encrypt=true;trustServerCertificate=false;loginTimeout=90;"
)

JDBC_PROPS = {
    "user": SQL_USER,
    "password": SQL_PASSWORD,
    "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
}

def read_table(query: str):
    return (spark.read
        .format("jdbc")
        .option("url", JDBC_URL)
        .option("query", query)
        .option("user", SQL_USER)
        .option("password", SQL_PASSWORD)
        .option("driver", JDBC_PROPS["driver"])
        .load()
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Two-writer race guard

# COMMAND ----------

if verify_order_landed:
    n = (read_table(
            f"SELECT COUNT(*) AS n FROM velora_oms.orders "
            f"WHERE order_date = '{snapshot_date.isoformat()}'"
        ).collect()[0]["n"])
    print(f"velora_oms.orders @ {snapshot_date} : {n} rows")
    if n == 0:
        raise RuntimeError(
            f"orders has 0 rows for {snapshot_date} — Function App hasn't fired yet "
            f"or fired and failed. Refusing to write inventory. "
            f"Set verify_order_landed=false to override."
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Existing-rows guard (idempotency)

# COMMAND ----------

existing = (read_table(
    f"SELECT COUNT(*) AS n FROM velora_pim.inventory_snapshot "
    f"WHERE snapshot_date = '{snapshot_date.isoformat()}'"
).collect()[0]["n"])
print(f"existing inventory_snapshot @ {snapshot_date} : {existing} rows")

if existing > 0 and not force:
    raise RuntimeError(
        f"inventory_snapshot already has {existing} rows for {snapshot_date}. "
        f"Re-run with force=true to wipe + rewrite."
    )

if existing > 0 and force:
    print(f"force=true → deleting {existing} existing rows for {snapshot_date}")
    # Spark JDBC reader does SELECT only; for mutating SQL we drop to
    # the driver-side JDBC DriverManager via py4j.
    sc = spark.sparkContext
    sc._gateway.jvm.java.lang.Class.forName(JDBC_PROPS["driver"])
    drv = sc._gateway.jvm.java.sql.DriverManager
    conn = drv.getConnection(JDBC_URL, SQL_USER, SQL_PASSWORD)
    try:
        stmt = conn.prepareStatement(
            "DELETE FROM velora_pim.inventory_snapshot WHERE snapshot_date = ?"
        )
        stmt.setDate(1, sc._gateway.jvm.java.sql.Date.valueOf(snapshot_date.isoformat()))
        deleted = stmt.executeUpdate()
        stmt.close()
        print(f"  deleted {deleted} rows")
    finally:
        conn.close()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read product + store grids

# COMMAND ----------

products = read_table(
    "SELECT product_id FROM velora_pim.products WHERE is_active = 1"
).cache()
stores = read_table(
    "SELECT store_id FROM velora_pim.stores WHERE is_active = 1"
).cache()

n_products = products.count()
n_stores = stores.count()
expected_rows = n_products * n_stores
print(f"products = {n_products} active SKUs")
print(f"stores   = {n_stores} active stores")
print(f"expected = {expected_rows} inventory rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Synthesize snapshot
# MAGIC
# MAGIC Deterministic per `(product_id, store_id, snapshot_date)` via `xxhash64`.
# MAGIC Re-running the same date produces structurally identical rows (modulo the
# MAGIC fresh `snapshot_id` UUIDs — natural key on (product, store, date) is
# MAGIC stable).

# COMMAND ----------

grid = products.crossJoin(stores).withColumn(
    "snapshot_date", F.lit(snapshot_date)
)

# Hash helpers — one per measure, salted to decorrelate.
def hmod(salt: str, mod_col):
    h = F.abs(F.xxhash64(F.col("product_id"), F.col("store_id"),
                         F.col("snapshot_date"), F.lit(salt)))
    return (h % mod_col).cast("int")

opening = hmod("OPENING", F.lit(150))                       # 0..149
sold = hmod("SOLD",       F.least(opening + F.lit(1), F.lit(50)))
returned_max = F.greatest(F.lit(1), (sold / F.lit(10)).cast("int"))
returned = hmod("RETURN",  returned_max)
closing = F.greatest(F.lit(0), opening - sold + returned)
reorder = hmod("REORDER", F.lit(30)) + F.lit(10)            # 10..39

snapshot = (grid
    .withColumn("opening_stock", opening)
    .withColumn("units_sold",    sold)
    .withColumn("units_returned", returned)
    .withColumn("closing_stock", closing)
    .withColumn("stockout_flag", (F.col("closing_stock") <= 0).cast("int"))
    .withColumn("reorder_point", reorder)
    .withColumn("snapshot_id",   F.expr("uuid()"))
    .withColumn("created_at",    F.current_timestamp())
    .withColumn("source_system", F.lit("VELORA_PIM"))
    .select(
        "snapshot_id", "product_id", "store_id", "snapshot_date",
        "opening_stock", "units_sold", "units_returned", "closing_stock",
        "stockout_flag", "reorder_point", "created_at", "source_system",
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write via JDBC

# COMMAND ----------

(snapshot
    .repartition(8)
    .write
    .format("jdbc")
    .option("url", JDBC_URL)
    .option("dbtable", "velora_pim.inventory_snapshot")
    .option("user", SQL_USER)
    .option("password", SQL_PASSWORD)
    .option("driver", JDBC_PROPS["driver"])
    .option("batchsize", "10000")
    .option("isolationLevel", "READ_COMMITTED")
    .mode("append")
    .save()
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify

# COMMAND ----------

verify = (read_table(
    f"SELECT COUNT(*) AS n, "
    f"COUNT(DISTINCT product_id) AS distinct_products, "
    f"COUNT(DISTINCT store_id) AS distinct_stores, "
    f"SUM(CAST(stockout_flag AS INT)) AS stockouts, "
    f"AVG(opening_stock) AS avg_opening "
    f"FROM velora_pim.inventory_snapshot "
    f"WHERE snapshot_date = '{snapshot_date.isoformat()}'"
).collect()[0])

print(f"rows              = {verify['n']}")
print(f"distinct products = {verify['distinct_products']}")
print(f"distinct stores   = {verify['distinct_stores']}")
print(f"stockouts         = {verify['stockouts']}")
print(f"avg opening_stock = {verify['avg_opening']:.1f}")

if verify["n"] != expected_rows:
    raise RuntimeError(
        f"row count mismatch: wrote {verify['n']}, expected {expected_rows}"
    )

dbutils.notebook.exit(str({
    "snapshot_date": str(snapshot_date),
    "rows_written": verify["n"],
    "distinct_products": verify["distinct_products"],
    "distinct_stores": verify["distinct_stores"],
}))
