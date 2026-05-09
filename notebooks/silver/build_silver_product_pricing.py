# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.product_pricing → silver.product_pricing + quarantine.product_pricing
# MAGIC
# MAGIC Reads `bronze.default.product_pricing` (append-only price-change log),
# MAGIC dedups by `pricing_id` keeping the latest `_bronze_timestamp`, runs DQ
# MAGIC rules, then MERGEs into `silver.default.product_pricing`. Rejected rows
# MAGIC are appended to `quarantine.default.product_pricing`.
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
# MAGIC - Silver is **deduplicated** on the business key (`pricing_id`).
# MAGIC - Silver is DQ-validated; bad rows are quarantined.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - Source is **append-only** at the OLTP — but `effective_to` flips from
# MAGIC   NULL to a date when a price is superseded, so we still MERGE rather
# MAGIC   than INSERT.
# MAGIC - Drives `gold.dim_product` SCD-2 list_price tracking.
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `pricing_id` IS NULL | `NULL_PRICING_ID` |
# MAGIC | `product_id` IS NULL | `NULL_PRODUCT_ID` |
# MAGIC | `product_id` not in `bronze.products` | `UNKNOWN_PRODUCT_ID` |
# MAGIC | `list_price` IS NULL OR `<= 0` | `INVALID_LIST_PRICE` |
# MAGIC | `pricing_type` not in (`STANDARD`,`PROMOTIONAL`,`CLEARANCE`) | `INVALID_PRICING_TYPE` |
# MAGIC | `effective_from` IS NULL | `NULL_EFFECTIVE_FROM` |

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

bronze_pricing_tbl = f"{bronze_catalog}.{schema_name}.product_pricing"
bronze_products_tbl = f"{bronze_catalog}.{schema_name}.products"
silver_pricing_tbl = f"{silver_catalog}.{schema_name}.product_pricing"
quarantine_pricing_tbl = f"{quarantine_catalog}.{schema_name}.product_pricing"

print(f"pipeline_run_id        = {pipeline_run_id}")
print(f"bronze_pricing_tbl     = {bronze_pricing_tbl}")
print(f"bronze_products_tbl    = {bronze_products_tbl}")
print(f"silver_pricing_tbl     = {silver_pricing_tbl}")
print(f"quarantine_pricing_tbl = {quarantine_pricing_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze product_pricing + dedup on `pricing_id`

# COMMAND ----------

df_bronze = spark.table(bronze_pricing_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_pricing_tbl}")

dedup_window = Window.partitionBy("pricing_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)
dedup_count = df_dedup.count()
print(f"After dedup on pricing_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build product-id reference set for FK validation

# COMMAND ----------

df_known_products = (
    spark.table(bronze_products_tbl)
        .select("product_id")
        .distinct()
)
known_product_count = df_known_products.count()
print(f"Reference set: {known_product_count:,} distinct product_ids in {bronze_products_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Tag each row with DQ flags

# COMMAND ----------

VALID_PRICING_TYPES = ["STANDARD", "PROMOTIONAL", "CLEARANCE"]

df_with_fk = (
    df_dedup.alias("pp")
        .join(
            df_known_products.alias("p").withColumn("_product_known", F.lit(True)),
            on="product_id",
            how="left",
        )
        .withColumn("_product_known", F.coalesce(F.col("_product_known"), F.lit(False)))
)

df_dq = (
    df_with_fk
        .withColumn("_v_null_pricing_id",     F.when(F.col("pricing_id").isNull(),                              F.lit("NULL_PRICING_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_product_id",     F.when(F.col("product_id").isNull(),                              F.lit("NULL_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_product",     F.when(F.col("product_id").isNotNull() & (~F.col("_product_known")), F.lit("UNKNOWN_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_list_price",  F.when(F.col("list_price").isNull() | (F.col("list_price") <= 0), F.lit("INVALID_LIST_PRICE")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_pricing_type",F.when(~F.col("pricing_type").isin(VALID_PRICING_TYPES),          F.lit("INVALID_PRICING_TYPE")).otherwise(F.lit(None)))
        .withColumn("_v_null_effective_from", F.when(F.col("effective_from").isNull(),                          F.lit("NULL_EFFECTIVE_FROM")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_pricing_id",
    "_v_null_product_id",
    "_v_unknown_product",
    "_v_invalid_list_price",
    "_v_invalid_pricing_type",
    "_v_null_effective_from",
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
        .drop("_violations_array", "_product_known", *violation_cols)
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
        .select(
            F.col("pricing_id"),
            F.col("product_id"),
            F.col("list_price"),
            F.col("cost_price"),
            F.col("effective_from"),
            F.col("effective_to"),
            F.col("pricing_type"),
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

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{silver_catalog}`.`{schema_name}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{quarantine_catalog}`.`{schema_name}`")

silver_table_exists = spark.catalog.tableExists(silver_pricing_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_pricing_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_pricing_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.product_pricing`

# COMMAND ----------

staging_view = "silver_product_pricing_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_pricing_tbl} AS tgt
USING {staging_view} AS src
ON tgt.pricing_id = src.pricing_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.product_pricing`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("pricing_id"), F.col("product_id"), F.col("list_price"),
                F.col("cost_price"), F.col("effective_from"), F.col("effective_to"),
                F.col("pricing_type"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_pim.product_pricing"))
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
            .saveAsTable(quarantine_pricing_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_pricing_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_pricing_tbl).count()
print(f"{silver_pricing_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_pricing_tbl).count()
    if spark.catalog.tableExists(quarantine_pricing_tbl) else 0
)
print(f"{quarantine_pricing_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_pricing_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
