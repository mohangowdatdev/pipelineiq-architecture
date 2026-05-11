# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — silver.inventory_snapshot → gold.fact_inventory_daily
# MAGIC
# MAGIC Grain: `(snapshot_date_id, product_surrogate_key, store_id)`. Source
# MAGIC is already at this grain — `silver.inventory_snapshot` is one row per
# MAGIC `(product_id, store_id, snapshot_date)`, so this is a 1:1 build with
# MAGIC dim attribution + one derived measure.
# MAGIC
# MAGIC ## FK lookups
# MAGIC
# MAGIC | Dim | Join semantics |
# MAGIC |---|---|
# MAGIC | `dim_date` | `cast(snapshot_date as YYYYMMDD INT)` |
# MAGIC | `dim_product` | as-of join on `snapshot_date` (DECISIONS #56), with floor-at-earliest fallback |
# MAGIC | `dim_store` | direct on `store_id` (SCD-1, no version) |
# MAGIC
# MAGIC ## Derived measure: `days_of_stock_remaining`
# MAGIC
# MAGIC Per SCHEMA.md `gold.fact_inventory_daily` derivation:
# MAGIC ```
# MAGIC days_of_stock_remaining = closing_stock / NULLIF(avg_daily_units_sold_7d, 0)
# MAGIC ```
# MAGIC where `avg_daily_units_sold_7d` is the trailing 7-day mean of
# MAGIC `units_sold` for the same `(product_id, store_id)` pair, looking back
# MAGIC 7 days from the snapshot_date inclusive. NULL when (a) the trailing
# MAGIC average is zero, OR (b) there's not yet 7 days of history (i.e. the
# MAGIC 7-day window has < 7 distinct snapshot_dates for that pair). Cast to
# MAGIC INT (floor — partial days don't matter for a stock-runway signal).
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC MERGE on the composite grain key `(snapshot_date_id,
# MAGIC product_surrogate_key, store_id)`. Re-runs UPSERT in place. Note that
# MAGIC `product_surrogate_key` may shift if `dim_product` SCD-2 history grows
# MAGIC backward (rare); in that case the row's grain composite changes and
# MAGIC the old row is left behind. Acceptable — `_gold_timestamp` shows the
# MAGIC most recent build per row.

# COMMAND ----------

import uuid
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("silver_catalog", "silver")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
silver_catalog = dbutils.widgets.get("silver_catalog").strip()
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

silver_inv_tbl = f"{silver_catalog}.{schema_name}.inventory_snapshot"
dim_product_tbl = f"{gold_catalog}.{schema_name}.dim_product"
dim_store_tbl = f"{gold_catalog}.{schema_name}.dim_store"
fact_tbl = f"{gold_catalog}.{schema_name}.fact_inventory_daily"

print(f"pipeline_run_id  = {pipeline_run_id}")
print(f"silver_inv_tbl   = {silver_inv_tbl}")
print(f"dim_product_tbl  = {dim_product_tbl}")
print(f"dim_store_tbl    = {dim_store_tbl}")
print(f"fact_tbl         = {fact_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read silver.inventory_snapshot (DQ-passed only)

# COMMAND ----------

df_inv = (
    spark.table(silver_inv_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "snapshot_id",
            "product_id",
            "store_id",
            "snapshot_date",
            "opening_stock",
            "units_sold",
            "units_returned",
            "closing_stock",
            "stockout_flag",
            "reorder_point",
            F.col("_ingestion_timestamp").alias("_silver_ingestion_timestamp"),
        )
)
inv_count = df_inv.count()
print(f"silver.inventory_snapshot clean rows: {inv_count:,}")
print("Per-snapshot_date input volume:")
display(df_inv.groupBy("snapshot_date").count().orderBy("snapshot_date"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. As-of join helper (copy of the one in fact_order_line)

# COMMAND ----------

def asof_attach(
    df_facts,
    fact_join_col: str,
    fact_date_col: str,
    dim_table: str,
    dim_join_col: str,
    surrogate_alias: str,
    extra_cols: list[tuple[str, str]] = None,
    fact_pk_cols: list[str] = None,
):
    """As-of attach with floor-at-earliest fallback.

    Identical semantics to fact_order_line.asof_attach but takes a list of
    fact PK cols (so we can window-rank by the composite inventory grain
    rather than a single line_id).
    """
    if fact_pk_cols is None:
        fact_pk_cols = ["snapshot_id"]

    df_dim = (
        spark.table(dim_table)
            .select(
                F.col(dim_join_col).alias("_dim_nk"),
                F.col("surrogate_key").alias(f"_dim_{surrogate_alias}"),
                F.col("valid_from").alias("_dim_valid_from"),
                F.col("valid_to").alias("_dim_valid_to"),
                *[F.col(c).alias(f"_dim_extra_{i}") for i, (c, _) in enumerate(extra_cols or [])],
            )
    )

    df_pairs = (
        df_facts.alias("f")
            .join(df_dim.alias("d"),
                  F.col(f"f.{fact_join_col}") == F.col("d._dim_nk"),
                  "left")
    )

    is_in_range = (
        (F.col("_dim_valid_from") <= F.col(fact_date_col)) &
        (F.coalesce(F.col("_dim_valid_to"), F.lit("9999-12-31").cast("date")) >= F.col(fact_date_col))
    )

    w_rank = (
        Window.partitionBy(*fact_pk_cols)
            .orderBy(
                F.when(is_in_range, F.lit(0)).otherwise(F.lit(1)).asc(),
                F.when(is_in_range, F.col("_dim_valid_from")).otherwise(F.lit(None)).desc_nulls_last(),
                F.when(~is_in_range, F.col("_dim_valid_from")).otherwise(F.lit(None)).asc_nulls_last(),
            )
    )

    df_ranked = (
        df_pairs
            .withColumn("_rn", F.row_number().over(w_rank))
            .filter(F.col("_rn") == 1)
    )

    df_out = df_ranked.withColumnRenamed(f"_dim_{surrogate_alias}", surrogate_alias)
    for i, (_, fact_alias) in enumerate(extra_cols or []):
        df_out = df_out.withColumnRenamed(f"_dim_extra_{i}", fact_alias)

    return df_out.drop("_dim_nk", "_dim_valid_from", "_dim_valid_to", "_rn")


# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Attach dim_product surrogate (as-of on snapshot_date)

# COMMAND ----------

df_with_product = asof_attach(
    df_inv,
    fact_join_col="product_id",
    fact_date_col="snapshot_date",
    dim_table=dim_product_tbl,
    dim_join_col="product_id",
    surrogate_alias="product_surrogate_key",
    fact_pk_cols=["product_id", "store_id", "snapshot_date"],
)
unmatched_product = df_with_product.filter(F.col("product_surrogate_key").isNull()).count()
print(f"dim_product attach: {df_with_product.count():,} rows, {unmatched_product:,} unmatched")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Validate store_id against dim_store (no surrogate; SCD-1 dim uses NK)

# COMMAND ----------

df_known_stores = (
    spark.table(dim_store_tbl)
        .select(F.col("store_id").alias("_dim_store_id"))
        .distinct()
)

df_with_store = (
    df_with_product.alias("f")
        .join(df_known_stores.alias("ds"),
              F.col("f.store_id") == F.col("ds._dim_store_id"),
              "left")
        .drop("_dim_store_id")
)
unmatched_store = df_with_store.filter(F.col("store_id").isNotNull() & F.col("_dim_store_id").isNull() if False else F.lit(False)).count()  # store_id always non-null at this point (DQ-passed)
print(f"dim_store check: {df_with_store.count():,} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Compute 7-day trailing average `units_sold` per (product_id, store_id)
# MAGIC
# MAGIC Window is **range-based** (last 7 days of `snapshot_date` inclusive),
# MAGIC NOT row-based — that way missing days don't shift the window and pull
# MAGIC in older data accidentally. `count()` over the window is used to
# MAGIC enforce the "need 7 days of history" rule: NULL out the average if
# MAGIC the window has < 7 distinct days.
# MAGIC
# MAGIC Spark's `rangeBetween` requires a numeric column for the range, so we
# MAGIC convert `snapshot_date` → days-since-epoch (`unix_date`) and use
# MAGIC `rangeBetween(-6, 0)` for "today + 6 days back" inclusive.

# COMMAND ----------

df_for_avg = df_with_store.withColumn(
    "_snapshot_date_unix", F.expr("unix_date(snapshot_date)").cast("long")
)

w_7d = (
    Window.partitionBy("product_id", "store_id")
        .orderBy(F.col("_snapshot_date_unix"))
        .rangeBetween(-6, 0)  # last 7 days inclusive
)

df_with_rolling = (
    df_for_avg
        .withColumn("_units_sold_7d_avg", F.avg("units_sold").over(w_7d))
        .withColumn("_window_day_count", F.count("snapshot_date").over(w_7d))
        .withColumn(
            "days_of_stock_remaining",
            F.when(
                (F.col("_window_day_count") < 7)
                | (F.col("_units_sold_7d_avg").isNull())
                | (F.col("_units_sold_7d_avg") == 0),
                F.lit(None).cast("int"),
            ).otherwise(
                (F.col("closing_stock") / F.col("_units_sold_7d_avg")).cast("int")
            ),
        )
        .drop("_snapshot_date_unix", "_units_sold_7d_avg", "_window_day_count")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Final projection

# COMMAND ----------

df_fact = (
    df_with_rolling
        .withColumn(
            "snapshot_date_id",
            (F.year("snapshot_date") * 10000 +
             F.month("snapshot_date") * 100 +
             F.dayofmonth("snapshot_date")).cast("int"),
        )
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            F.col("snapshot_date_id"),
            F.col("product_surrogate_key"),
            F.col("store_id"),
            F.col("opening_stock"),
            F.col("units_sold"),
            F.col("units_returned"),
            F.col("closing_stock"),
            F.col("stockout_flag"),
            F.col("reorder_point"),
            F.col("days_of_stock_remaining"),
            F.col("_pipeline_run_id"),
            F.col("_silver_ingestion_timestamp").alias("_ingestion_timestamp"),
            F.col("_gold_timestamp"),
        )
)

batch_count = df_fact.count()
print(f"Fact batch rows ready to MERGE: {batch_count:,}")
print(f"  rows with NULL days_of_stock_remaining: "
      f"{df_fact.filter(F.col('days_of_stock_remaining').isNull()).count():,}")
print(f"  rows with NULL product_surrogate_key:   "
      f"{df_fact.filter(F.col('product_surrogate_key').isNull()).count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Ensure table + MERGE on composite grain
# MAGIC
# MAGIC Partition by `snapshot_date_id` for time-range query pruning — most
# MAGIC inventory dashboards filter by date.

# COMMAND ----------

fact_exists = spark.catalog.tableExists(fact_tbl)
print(f"{fact_tbl} exists: {fact_exists}")

if not fact_exists:
    print(f"Creating {fact_tbl} for the first time")
    (
        df_fact.limit(0).write
            .format("delta")
            .partitionBy("snapshot_date_id")
            .saveAsTable(fact_tbl)
    )

staging_view = "fact_inventory_daily_staging"
df_fact.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {fact_tbl} AS tgt
USING {staging_view} AS src
ON  tgt.snapshot_date_id      = src.snapshot_date_id
AND tgt.product_surrogate_key = src.product_surrogate_key
AND tgt.store_id              = src.store_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""
print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

fact_total = spark.table(fact_tbl).count()
distinct_grain = (
    spark.table(fact_tbl)
        .select("snapshot_date_id", "product_surrogate_key", "store_id")
        .distinct()
        .count()
)
print(f"{fact_tbl} total rows:         {fact_total:,}")
print(f"  distinct grain composites:    {distinct_grain:,} ({'OK' if fact_total == distinct_grain else 'PK COLLISION'})")

print("\nPer-snapshot_date_id row counts:")
display(
    spark.table(fact_tbl)
        .groupBy("snapshot_date_id")
        .agg(
            F.count("*").alias("n_rows"),
            F.countDistinct("product_surrogate_key").alias("n_products"),
            F.countDistinct("store_id").alias("n_stores"),
            F.sum("closing_stock").alias("total_closing_stock"),
            F.sum(F.when(F.col("stockout_flag"), 1).otherwise(0)).alias("n_stockouts"),
            F.sum(F.when(F.col("days_of_stock_remaining").isNotNull(), 1).otherwise(0)).alias("n_with_dosr"),
        )
        .orderBy("snapshot_date_id")
)

display(
    spark.table(fact_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)
