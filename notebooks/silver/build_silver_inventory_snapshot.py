# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.inventory_snapshot → silver.inventory_snapshot + quarantine.inventory_snapshot
# MAGIC
# MAGIC Reads `bronze.default.inventory_snapshot` (daily full snapshot — 189K
# MAGIC rows per source date), dedups on the **composite business key**
# MAGIC `(product_id, store_id, snapshot_date)` keeping the latest
# MAGIC `_bronze_timestamp`, runs DQ rules, then MERGEs the clean batch into
# MAGIC `silver.default.inventory_snapshot`. Rejected rows are appended to
# MAGIC `quarantine.default.inventory_snapshot`.
# MAGIC
# MAGIC ## Why composite-key dedup, not `snapshot_id`?
# MAGIC
# MAGIC Per SCHEMA.md: `snapshot_id` is unique at source, BUT a re-extracted
# MAGIC date (re-run of the export script for a date that already landed)
# MAGIC produces a second Bronze row with the **same** `(product_id, store_id,
# MAGIC snapshot_date)` and a **different** `snapshot_id`. Dedup on the
# MAGIC composite key collapses these correctly; dedup on `snapshot_id` would
# MAGIC keep both. The Silver MERGE key is the same composite tuple, so
# MAGIC re-runs are idempotent at the Silver layer too.
# MAGIC
# MAGIC ## Partitioning
# MAGIC
# MAGIC Partitioned by `snapshot_date` (NOT by `_silver_date` like the smaller
# MAGIC silver tables). Reasoning: `gold.fact_inventory_daily` reads this table
# MAGIC by `snapshot_date` for its as-of join to `dim_product` + 7-day rolling
# MAGIC average. Partition pruning on `snapshot_date` is the load-bearing perf
# MAGIC optimization at this volume (756K rows for 4 days, growing to ~2.6M
# MAGIC for 14 days; would grow to ~70M / year if extended). `_silver_date`
# MAGIC is still written as a column for re-run lineage but not used as the
# MAGIC partition column.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `pipeline_run_id` | uuid | yes — ADF passes this; falls back to a random uuid for manual runs |
# MAGIC | `bronze_catalog` | `bronze` | no |
# MAGIC | `silver_catalog` | `silver` | no |
# MAGIC | `quarantine_catalog` | `quarantine` | no |
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `snapshot_id` IS NULL | `NULL_SNAPSHOT_ID` |
# MAGIC | `product_id` IS NULL | `NULL_PRODUCT_ID` |
# MAGIC | `store_id` IS NULL | `NULL_STORE_ID` |
# MAGIC | `snapshot_date` IS NULL | `NULL_SNAPSHOT_DATE` |
# MAGIC | `product_id` not in `bronze.products` | `UNKNOWN_PRODUCT_ID` |
# MAGIC | `store_id` not in `bronze.stores` | `UNKNOWN_STORE_ID` |
# MAGIC | `opening_stock < 0` OR `closing_stock < 0` | `NEGATIVE_STOCK` |
# MAGIC
# MAGIC Multiple violations on the same row are concatenated with `;`.

# COMMAND ----------

import uuid
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("bronze_catalog", "bronze")
dbutils.widgets.text("silver_catalog", "silver")
dbutils.widgets.text("quarantine_catalog", "quarantine")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
bronze_catalog = dbutils.widgets.get("bronze_catalog").strip()
silver_catalog = dbutils.widgets.get("silver_catalog").strip()
quarantine_catalog = dbutils.widgets.get("quarantine_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

bronze_inv_tbl = f"{bronze_catalog}.{schema_name}.inventory_snapshot"
bronze_products_tbl = f"{bronze_catalog}.{schema_name}.products"
bronze_stores_tbl = f"{bronze_catalog}.{schema_name}.stores"
silver_inv_tbl = f"{silver_catalog}.{schema_name}.inventory_snapshot"
quarantine_inv_tbl = f"{quarantine_catalog}.{schema_name}.inventory_snapshot"

print(f"pipeline_run_id        = {pipeline_run_id}")
print(f"bronze_inv_tbl         = {bronze_inv_tbl}")
print(f"bronze_products_tbl    = {bronze_products_tbl}")
print(f"bronze_stores_tbl      = {bronze_stores_tbl}")
print(f"silver_inv_tbl         = {silver_inv_tbl}")
print(f"quarantine_inv_tbl     = {quarantine_inv_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze inventory_snapshot + dedup on composite key

# COMMAND ----------

df_bronze = spark.table(bronze_inv_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_inv_tbl}")

dedup_window = (
    Window
        .partitionBy("product_id", "store_id", "snapshot_date")
        .orderBy(F.col("_bronze_timestamp").desc())
)

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)

dedup_count = df_dedup.count()
print(
    f"After dedup on (product_id, store_id, snapshot_date): {dedup_count:,} rows "
    f"({bronze_count - dedup_count:,} duplicates collapsed)"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build FK reference sets for `product_id` and `store_id`

# COMMAND ----------

df_known_products = (
    spark.table(bronze_products_tbl)
        .select("product_id")
        .distinct()
)
known_product_count = df_known_products.count()
print(f"Reference set: {known_product_count:,} distinct product_ids in {bronze_products_tbl}")

df_known_stores = (
    spark.table(bronze_stores_tbl)
        .select("store_id")
        .distinct()
)
known_store_count = df_known_stores.count()
print(f"Reference set: {known_store_count:,} distinct store_ids in {bronze_stores_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Tag each row with DQ flags

# COMMAND ----------

df_with_fk = (
    df_dedup.alias("inv")
        .join(
            df_known_products.alias("p").withColumn("_product_known", F.lit(True)),
            on="product_id",
            how="left",
        )
        .withColumn("_product_known", F.coalesce(F.col("_product_known"), F.lit(False)))
        .join(
            df_known_stores.alias("s").withColumn("_store_known", F.lit(True)),
            on="store_id",
            how="left",
        )
        .withColumn("_store_known", F.coalesce(F.col("_store_known"), F.lit(False)))
)

df_dq = (
    df_with_fk
        .withColumn("_v_null_snapshot_id",   F.when(F.col("snapshot_id").isNull(),                                  F.lit("NULL_SNAPSHOT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_product_id",    F.when(F.col("product_id").isNull(),                                   F.lit("NULL_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_store_id",      F.when(F.col("store_id").isNull(),                                     F.lit("NULL_STORE_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_snapshot_date", F.when(F.col("snapshot_date").isNull(),                                F.lit("NULL_SNAPSHOT_DATE")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_product",    F.when(F.col("product_id").isNotNull() & (~F.col("_product_known")),   F.lit("UNKNOWN_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_store",      F.when(F.col("store_id").isNotNull()   & (~F.col("_store_known")),     F.lit("UNKNOWN_STORE_ID")).otherwise(F.lit(None)))
        .withColumn(
            "_v_negative_stock",
            F.when(
                (F.col("opening_stock") < 0) | (F.col("closing_stock") < 0),
                F.lit("NEGATIVE_STOCK"),
            ).otherwise(F.lit(None)),
        )
)

violation_cols = [
    "_v_null_snapshot_id",
    "_v_null_product_id",
    "_v_null_store_id",
    "_v_null_snapshot_date",
    "_v_unknown_product",
    "_v_unknown_store",
    "_v_negative_stock",
]

df_tagged = (
    df_dq
        .withColumn(
            "_violations_array",
            F.array_compact(F.array(*[F.col(c) for c in violation_cols])),
        )
        .withColumn(
            "dq_rejection_reason",
            F.when(F.size("_violations_array") == 0, F.lit(None))
             .otherwise(F.concat_ws(";", F.col("_violations_array"))),
        )
        .withColumn("dq_passed", F.col("dq_rejection_reason").isNull())
        .drop("_violations_array", "_product_known", "_store_known", *violation_cols)
)

passed_count = df_tagged.filter(F.col("dq_passed")).count()
failed_count = df_tagged.filter(~F.col("dq_passed")).count()
print(f"DQ result: {passed_count:,} passed / {failed_count:,} rejected")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Project to the Silver schema

# COMMAND ----------

df_silver_batch = (
    df_tagged.filter(F.col("dq_passed"))
        .withColumn("_silver_timestamp", F.current_timestamp())
        .withColumn("stockout_flag", F.col("stockout_flag").cast("boolean"))
        .select(
            F.col("snapshot_id"),
            F.col("product_id"),
            F.col("store_id"),
            F.col("snapshot_date"),
            F.col("opening_stock"),
            F.col("units_sold"),
            F.col("units_returned"),
            F.col("closing_stock"),
            F.col("stockout_flag"),
            F.col("reorder_point"),
            F.col("dq_passed"),
            F.col("dq_rejection_reason"),
            F.col("_source_file"),
            F.lit(pipeline_run_id).alias("_pipeline_run_id"),
            F.col("_ingestion_timestamp"),
            F.col("_silver_timestamp"),
            F.to_date(F.col("_silver_timestamp")).alias("_silver_date"),
        )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Ensure schemas + create Silver table on first run
# MAGIC
# MAGIC Partitioned by `snapshot_date` (see header docstring for rationale).

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{silver_catalog}`.`{schema_name}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{quarantine_catalog}`.`{schema_name}`")

silver_table_exists = spark.catalog.tableExists(silver_inv_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_inv_tbl} with the Silver schema, partitioned by snapshot_date")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("snapshot_date")
            .saveAsTable(silver_inv_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.inventory_snapshot`
# MAGIC
# MAGIC Merge key is the composite `(product_id, store_id, snapshot_date)` —
# MAGIC NOT `snapshot_id`. See header docstring.

# COMMAND ----------

staging_view = "silver_inventory_snapshot_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_inv_tbl} AS tgt
USING {staging_view} AS src
ON  tgt.product_id    = src.product_id
AND tgt.store_id      = src.store_id
AND tgt.snapshot_date = src.snapshot_date
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.inventory_snapshot`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("snapshot_id"), F.col("product_id"), F.col("store_id"),
                F.col("snapshot_date"),
                F.col("opening_stock"), F.col("units_sold"),
                F.col("units_returned"), F.col("closing_stock"),
                F.col("stockout_flag"), F.col("reorder_point"),
                F.col("_source_file"),
            )))
            .withColumn("source_table", F.lit("velora_pim.inventory_snapshot"))
            .select(
                "quarantine_id", "rejection_reason", "pipeline_run_id",
                "rejected_at", "raw_record", "source_table",
            )
    )

    (
        df_quarantine.write
            .format("delta")
            .mode("append")
            .option("mergeSchema", "true")
            .saveAsTable(quarantine_inv_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_inv_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_inv_tbl).count()
print(f"{silver_inv_tbl} now holds {silver_total:,} rows total")

print("\nRow counts per snapshot_date:")
display(
    spark.table(silver_inv_tbl)
        .groupBy("snapshot_date")
        .agg(F.count("*").alias("n"))
        .orderBy("snapshot_date")
)

quarantine_total = (
    spark.table(quarantine_inv_tbl).count()
    if spark.catalog.tableExists(quarantine_inv_tbl) else 0
)
print(f"{quarantine_inv_tbl} now holds {quarantine_total:,} rows total")
