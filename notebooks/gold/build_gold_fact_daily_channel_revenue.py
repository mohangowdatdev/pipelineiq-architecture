# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — gold.fact_order_line → gold.fact_daily_channel_revenue
# MAGIC
# MAGIC Pre-aggregated rollup. Grain:
# MAGIC `(summary_date_id, channel_id, category_id, territory_id)`.
# MAGIC
# MAGIC ## Source pattern (Gold→Gold)
# MAGIC
# MAGIC `gold.fact_order_line` is the single source of truth for all
# MAGIC measure definitions. This rollup just `GROUP BY`s the four grain
# MAGIC columns. Joining to `gold.dim_product` only to get `category_id`
# MAGIC (the fact carries `product_surrogate_key`, not `category_id`
# MAGIC directly).
# MAGIC
# MAGIC ## Measures (per SCHEMA.md)
# MAGIC
# MAGIC | Measure | Formula |
# MAGIC |---|---|
# MAGIC | `total_orders` | `COUNT(DISTINCT order_id)` over the grain |
# MAGIC | `total_lines` | `COUNT(*)` over the grain |
# MAGIC | `total_units_sold` | `SUM(quantity_ordered)` |
# MAGIC | `gross_revenue_inr` | `SUM(quantity_ordered * unit_price_at_sale)` (pre-discount) |
# MAGIC | `total_discount_inr` | `SUM(discount_amount)` |
# MAGIC | `net_revenue_inr` | `SUM(net_revenue_inr)` (= post-discount, pre-tax) |
# MAGIC | `total_tax_inr` | `SUM(tax_amount)` |
# MAGIC | `avg_order_value_inr` | `net_revenue_inr / total_orders` |
# MAGIC | `return_rate_pct` | `100.0 * COUNT(*) WHERE status_id='RETURNED' / total_lines` |
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC MERGE on the 4-key grain. Re-runs UPSERT — measures are
# MAGIC deterministic given the same `fact_order_line` state.

# COMMAND ----------

import uuid
from pyspark.sql import functions as F

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

fact_line_tbl = f"{gold_catalog}.{schema_name}.fact_order_line"
dim_product_tbl = f"{gold_catalog}.{schema_name}.dim_product"
rollup_tbl = f"{gold_catalog}.{schema_name}.fact_daily_channel_revenue"

print(f"pipeline_run_id   = {pipeline_run_id}")
print(f"fact_line_tbl     = {fact_line_tbl}")
print(f"dim_product_tbl   = {dim_product_tbl}")
print(f"rollup_tbl        = {rollup_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read fact_order_line + attach category_id from dim_product

# COMMAND ----------

df_fact = spark.table(fact_line_tbl)
fact_count = df_fact.count()
print(f"fact_order_line rows: {fact_count:,}")

df_dim_product = (
    spark.table(dim_product_tbl)
        .select(
            F.col("surrogate_key").alias("_dp_sk"),
            F.col("category_id").alias("_dp_category_id"),
        )
)

df_fact_enriched = (
    df_fact.alias("f")
        .join(df_dim_product.alias("dp"),
              F.col("f.product_surrogate_key") == F.col("dp._dp_sk"),
              "left")
        .withColumn("category_id", F.col("_dp_category_id"))
        .drop("_dp_sk", "_dp_category_id")
)

unmatched_category = df_fact_enriched.filter(F.col("category_id").isNull()).count()
print(f"  rows missing category_id (unmatched dim_product): {unmatched_category:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Aggregate to grain

# COMMAND ----------

df_rollup = (
    df_fact_enriched
        .groupBy(
            F.col("order_date_id").alias("summary_date_id"),
            F.col("channel_id"),
            F.col("category_id"),
            F.col("territory_id"),
        )
        .agg(
            F.countDistinct("order_id").alias("total_orders"),
            F.count(F.lit(1)).alias("total_lines"),
            F.sum("quantity_ordered").alias("total_units_sold"),
            F.round(F.sum(F.col("quantity_ordered") * F.col("unit_price_at_sale")), 2).alias("gross_revenue_inr"),
            F.round(F.sum("discount_amount"), 2).alias("total_discount_inr"),
            F.round(F.sum("net_revenue_inr"), 2).alias("net_revenue_inr"),
            F.round(F.sum("tax_amount"), 2).alias("total_tax_inr"),
            F.sum(F.when(F.col("status_id") == "RETURNED", F.lit(1)).otherwise(F.lit(0))).alias("_returned_lines"),
        )
        .withColumn(
            "avg_order_value_inr",
            F.round(F.col("net_revenue_inr") / F.col("total_orders"), 2),
        )
        .withColumn(
            "return_rate_pct",
            F.round(100.0 * F.col("_returned_lines") / F.col("total_lines"), 2),
        )
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            "summary_date_id",
            "channel_id",
            "category_id",
            "territory_id",
            "total_orders",
            "total_lines",
            "total_units_sold",
            "gross_revenue_inr",
            "total_discount_inr",
            "net_revenue_inr",
            "total_tax_inr",
            "avg_order_value_inr",
            "return_rate_pct",
            "_pipeline_run_id",
            "_gold_timestamp",
        )
)

rollup_count = df_rollup.count()
print(f"Rollup rows ready to MERGE: {rollup_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Ensure table + MERGE on the 4-key grain

# COMMAND ----------

rollup_exists = spark.catalog.tableExists(rollup_tbl)
print(f"{rollup_tbl} exists: {rollup_exists}")

if not rollup_exists:
    print(f"Creating {rollup_tbl} for the first time")
    (
        df_rollup.limit(0).write
            .format("delta")
            .partitionBy("summary_date_id")
            .saveAsTable(rollup_tbl)
    )

staging_view = "fact_daily_channel_revenue_staging"
df_rollup.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {rollup_tbl} AS tgt
USING {staging_view} AS src
ON  tgt.summary_date_id = src.summary_date_id
AND tgt.channel_id      = src.channel_id
AND tgt.category_id     <=> src.category_id
AND tgt.territory_id    <=> src.territory_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""
print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Verify

# COMMAND ----------

rollup_total = spark.table(rollup_tbl).count()
print(f"{rollup_tbl} total rows: {rollup_total:,}")

# Reconciliation against fact_order_line totals
print("\nReconciliation: rollup totals vs fact_order_line totals")
fact_totals = spark.table(fact_line_tbl).agg(
    F.sum("quantity_ordered").alias("fact_units"),
    F.round(F.sum("line_total_inr"), 2).alias("fact_line_total"),
    F.round(F.sum("net_revenue_inr"), 2).alias("fact_net_revenue"),
).collect()[0]
rollup_totals = spark.table(rollup_tbl).agg(
    F.sum("total_units_sold").alias("rollup_units"),
    F.round(F.sum("net_revenue_inr"), 2).alias("rollup_net_revenue"),
).collect()[0]
print(f"  fact units:        {fact_totals['fact_units']:,}")
print(f"  rollup units:      {rollup_totals['rollup_units']:,}  ({'OK' if fact_totals['fact_units'] == rollup_totals['rollup_units'] else 'MISMATCH'})")
print(f"  fact net_revenue:  {fact_totals['fact_net_revenue']:,}")
print(f"  rollup net_revenue:{rollup_totals['rollup_net_revenue']:,}  ({'OK' if fact_totals['fact_net_revenue'] == rollup_totals['rollup_net_revenue'] else 'MISMATCH'})")

# Top channels by revenue
print("\nTop channels by net revenue:")
display(
    spark.table(rollup_tbl)
        .groupBy("channel_id")
        .agg(F.round(F.sum("net_revenue_inr"), 2).alias("net_revenue"))
        .orderBy(F.col("net_revenue").desc())
)

display(
    spark.table(rollup_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)
