# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.order_lines → silver.order_lines + quarantine.order_lines
# MAGIC
# MAGIC Reads `bronze.default.order_lines` (append-only history), dedups by
# MAGIC `line_id` keeping the latest `_bronze_timestamp`, runs DQ rules, then
# MAGIC MERGEs the clean batch into `silver.default.order_lines`. Rejected rows
# MAGIC are appended to `quarantine.default.order_lines` with the rejection
# MAGIC reason and the original record JSON-serialised in `raw_record`.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `pipeline_run_id` | uuid | yes — ADF passes this; falls back to a random uuid for manual runs |
# MAGIC | `bronze_catalog` | `bronze` | no — default set |
# MAGIC | `silver_catalog` | `silver` | no — default set |
# MAGIC | `quarantine_catalog` | `quarantine` | no — default set |
# MAGIC
# MAGIC ## Medallion contract (per CLAUDE.md)
# MAGIC
# MAGIC - Silver is **deduplicated** on the business key (`line_id`).
# MAGIC - Silver is **DQ-validated** — each row carries `dq_passed` + `dq_rejection_reason`.
# MAGIC - Bad rows are quarantined, never silently dropped.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - FK validation uses the Bronze reference set (always available; matches
# MAGIC   the convention established by `build_silver_orders.py`).
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `line_id` IS NULL | `NULL_LINE_ID` |
# MAGIC | `order_id` IS NULL | `NULL_ORDER_ID` |
# MAGIC | `product_id` IS NULL | `NULL_PRODUCT_ID` |
# MAGIC | `order_id` not in `bronze.orders` | `UNKNOWN_ORDER_ID` |
# MAGIC | `product_id` not in `bronze.products` | `UNKNOWN_PRODUCT_ID` |
# MAGIC | `quantity` IS NULL OR `<= 0` | `INVALID_QUANTITY` |
# MAGIC | `unit_price` IS NULL OR `<= 0` | `INVALID_UNIT_PRICE` |
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

bronze_order_lines_tbl = f"{bronze_catalog}.{schema_name}.order_lines"
bronze_orders_tbl = f"{bronze_catalog}.{schema_name}.orders"
bronze_products_tbl = f"{bronze_catalog}.{schema_name}.products"
silver_order_lines_tbl = f"{silver_catalog}.{schema_name}.order_lines"
quarantine_order_lines_tbl = f"{quarantine_catalog}.{schema_name}.order_lines"

print(f"pipeline_run_id          = {pipeline_run_id}")
print(f"bronze_order_lines_tbl   = {bronze_order_lines_tbl}")
print(f"bronze_orders_tbl        = {bronze_orders_tbl}")
print(f"bronze_products_tbl      = {bronze_products_tbl}")
print(f"silver_order_lines_tbl   = {silver_order_lines_tbl}")
print(f"quarantine_order_lines_tbl = {quarantine_order_lines_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze order_lines + dedup on `line_id`

# COMMAND ----------

df_bronze = spark.table(bronze_order_lines_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_order_lines_tbl}")

dedup_window = Window.partitionBy("line_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)

dedup_count = df_dedup.count()
print(f"After dedup on line_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build FK reference sets for `order_id` and `product_id`

# COMMAND ----------

df_known_orders = (
    spark.table(bronze_orders_tbl)
        .select("order_id")
        .distinct()
)
known_order_count = df_known_orders.count()
print(f"Reference set: {known_order_count:,} distinct order_ids in {bronze_orders_tbl}")

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

df_with_fk = (
    df_dedup.alias("ol")
        .join(
            df_known_orders.alias("o").withColumn("_order_known", F.lit(True)),
            on="order_id",
            how="left",
        )
        .withColumn("_order_known", F.coalesce(F.col("_order_known"), F.lit(False)))
        .join(
            df_known_products.alias("p").withColumn("_product_known", F.lit(True)),
            on="product_id",
            how="left",
        )
        .withColumn("_product_known", F.coalesce(F.col("_product_known"), F.lit(False)))
)

df_dq = (
    df_with_fk
        .withColumn("_v_null_line_id",     F.when(F.col("line_id").isNull(),                                  F.lit("NULL_LINE_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_order_id",    F.when(F.col("order_id").isNull(),                                 F.lit("NULL_ORDER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_product_id",  F.when(F.col("product_id").isNull(),                               F.lit("NULL_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_order",    F.when(F.col("order_id").isNotNull()   & (~F.col("_order_known")),   F.lit("UNKNOWN_ORDER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_product",  F.when(F.col("product_id").isNotNull() & (~F.col("_product_known")), F.lit("UNKNOWN_PRODUCT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_quantity", F.when(F.col("quantity").isNull()   | (F.col("quantity")   <= 0),  F.lit("INVALID_QUANTITY")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_unit_price", F.when(F.col("unit_price").isNull() | (F.col("unit_price") <= 0), F.lit("INVALID_UNIT_PRICE")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_line_id",
    "_v_null_order_id",
    "_v_null_product_id",
    "_v_unknown_order",
    "_v_unknown_product",
    "_v_invalid_quantity",
    "_v_invalid_unit_price",
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
        .drop("_violations_array", "_order_known", "_product_known", *violation_cols)
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
            F.col("line_id"),
            F.col("order_id"),
            F.col("product_id"),
            F.col("quantity"),
            F.col("unit_price"),
            F.col("discount_amt"),
            F.col("line_total"),
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

silver_table_exists = spark.catalog.tableExists(silver_order_lines_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_order_lines_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_order_lines_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.order_lines`

# COMMAND ----------

staging_view = "silver_order_lines_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_order_lines_tbl} AS tgt
USING {staging_view} AS src
ON tgt.line_id = src.line_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.order_lines`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("line_id"), F.col("order_id"), F.col("product_id"),
                F.col("quantity"), F.col("unit_price"), F.col("discount_amt"),
                F.col("line_total"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_oms.order_lines"))
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
            .saveAsTable(quarantine_order_lines_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_order_lines_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_order_lines_tbl).count()
print(f"{silver_order_lines_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_order_lines_tbl).count()
    if spark.catalog.tableExists(quarantine_order_lines_tbl) else 0
)
print(f"{quarantine_order_lines_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_order_lines_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
