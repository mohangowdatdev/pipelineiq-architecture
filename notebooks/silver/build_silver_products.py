# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.products → silver.products + quarantine.products
# MAGIC
# MAGIC Reads `bronze.default.products` (append-only history), dedups by
# MAGIC `product_id` keeping the latest `_bronze_timestamp`, runs DQ rules, then
# MAGIC MERGEs the clean batch into `silver.default.products`. Rejected rows are
# MAGIC appended to `quarantine.default.products`.
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
# MAGIC ## Medallion contract (per CLAUDE.md)
# MAGIC
# MAGIC - Silver is **deduplicated** on the business key (`product_id`).
# MAGIC - Silver is **DQ-validated** — each row carries `dq_passed` + `dq_rejection_reason`.
# MAGIC - Bad rows are quarantined, never silently dropped.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - **No SCD tracking** on this table — the only SCD-2 attr in
# MAGIC   `gold.dim_product` is `list_price`, which is sourced from
# MAGIC   `silver.product_pricing`.
# MAGIC - `list_price` is **NOT** in this table — it lives in `silver.product_pricing`
# MAGIC   (the source preserves price history as an event log).
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `product_id` IS NULL | `NULL_PRODUCT_ID` |
# MAGIC | `sku` IS NULL | `NULL_SKU` |
# MAGIC | `division` not in 5-known-set | `INVALID_DIVISION` |
# MAGIC | `category_id` IS NULL | `NULL_CATEGORY_ID` |
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

bronze_products_tbl = f"{bronze_catalog}.{schema_name}.products"
silver_products_tbl = f"{silver_catalog}.{schema_name}.products"
quarantine_products_tbl = f"{quarantine_catalog}.{schema_name}.products"

print(f"pipeline_run_id         = {pipeline_run_id}")
print(f"bronze_products_tbl     = {bronze_products_tbl}")
print(f"silver_products_tbl     = {silver_products_tbl}")
print(f"quarantine_products_tbl = {quarantine_products_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze products + dedup on `product_id`

# COMMAND ----------

df_bronze = spark.table(bronze_products_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_products_tbl}")

dedup_window = Window.partitionBy("product_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)
dedup_count = df_dedup.count()
print(f"After dedup on product_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Tag each row with DQ flags

# COMMAND ----------

VALID_DIVISIONS = [
    "CONSUMER_ELECTRONICS",
    "HOME_APPLIANCES",
    "PERSONAL_CARE",
    "SPORTS_FITNESS",
    "PREMIUM_ACCESSORIES",
]

df_dq = (
    df_dedup
        .withColumn("_v_null_product_id",  F.when(F.col("product_id").isNull(),       F.lit("NULL_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_sku",         F.when(F.col("sku").isNull(),              F.lit("NULL_SKU")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_division", F.when(~F.col("division").isin(VALID_DIVISIONS), F.lit("INVALID_DIVISION")).otherwise(F.lit(None)))
        .withColumn("_v_null_category_id", F.when(F.col("category_id").isNull(),      F.lit("NULL_CATEGORY_ID")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_product_id",
    "_v_null_sku",
    "_v_invalid_division",
    "_v_null_category_id",
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
        .drop("_violations_array", *violation_cols)
)

passed_count = df_tagged.filter(F.col("dq_passed")).count()
failed_count = df_tagged.filter(~F.col("dq_passed")).count()
print(f"DQ result: {passed_count:,} passed / {failed_count:,} rejected")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Project to the Silver schema

# COMMAND ----------

df_silver_batch = (
    df_tagged.filter(F.col("dq_passed"))
        .withColumn("_silver_timestamp", F.current_timestamp())
        .select(
            F.col("product_id"),
            F.col("sku"),
            F.col("product_name"),
            F.col("category_id"),
            F.col("division"),
            F.col("brand"),
            F.col("is_active"),
            F.col("launched_date"),
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
# MAGIC ## 4. Ensure schemas + create Silver table on first run

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{silver_catalog}`.`{schema_name}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{quarantine_catalog}`.`{schema_name}`")

silver_table_exists = spark.catalog.tableExists(silver_products_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_products_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_products_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE into `silver.products`

# COMMAND ----------

staging_view = "silver_products_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_products_tbl} AS tgt
USING {staging_view} AS src
ON tgt.product_id = src.product_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Append rejected rows to `quarantine.products`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("product_id"), F.col("sku"), F.col("product_name"),
                F.col("category_id"), F.col("division"), F.col("brand"),
                F.col("is_active"), F.col("launched_date"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_pim.products"))
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
            .saveAsTable(quarantine_products_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_products_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Verify

# COMMAND ----------

silver_total = spark.table(silver_products_tbl).count()
print(f"{silver_products_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_products_tbl).count()
    if spark.catalog.tableExists(quarantine_products_tbl) else 0
)
print(f"{quarantine_products_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_products_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
