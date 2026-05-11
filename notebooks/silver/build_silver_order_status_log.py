# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.order_status_log → silver.order_status_log + quarantine.order_status_log
# MAGIC
# MAGIC Trailing-edge silver — no current Gold consumer. Future
# MAGIC `gold.fact_order_status_transitions` would source from this table for
# MAGIC status-flow analytics (e.g. avg time PENDING→DELIVERED, cancellation
# MAGIC funnel). Built now so the medallion contract holds end-to-end.
# MAGIC
# MAGIC Reads `bronze.default.order_status_log` (append-only event log at
# MAGIC source), dedups by `log_id` keeping the latest `_bronze_timestamp`,
# MAGIC runs DQ rules, MERGEs into `silver.default.order_status_log`.
# MAGIC
# MAGIC ## Status set
# MAGIC
# MAGIC SCHEMA.md `velora_oms.orders.status` lists 6 states: PENDING,
# MAGIC PROCESSING, SHIPPED, DELIVERED, CANCELLED, RETURNED. The generator
# MAGIC additionally emits **`RETURN_INITIATED`** as a transitional state
# MAGIC between DELIVERED and RETURNED (`generator/status_updates.py` +
# MAGIC `_TERMINAL_STATUSES` set). The valid set used here is the **union of
# MAGIC both** so Silver doesn't reject genuine generator output. SCHEMA.md
# MAGIC and `gold.dim_order_status` should pick up the 7th state in a future
# MAGIC pass — flagged in this notebook's DQ rule.
# MAGIC
# MAGIC `from_status` is **allowed to be NULL** — it represents the
# MAGIC conceptual order-creation event (NULL → PENDING). The current
# MAGIC generator never emits NULL `from_status` rows, but the Silver schema
# MAGIC tolerates them for future-proofing.
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
# MAGIC ## DQ rules applied (per SCHEMA.md `silver.order_status_log`)
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `log_id` IS NULL | `NULL_LOG_ID` |
# MAGIC | `order_id` IS NULL | `NULL_ORDER_ID` |
# MAGIC | `to_status` IS NULL | `NULL_TO_STATUS` |
# MAGIC | `order_id` not in `bronze.orders` | `UNKNOWN_ORDER_ID` |
# MAGIC | `from_status` not in known set (when not NULL) OR `to_status` not in known set | `INVALID_STATUS` |
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

bronze_log_tbl = f"{bronze_catalog}.{schema_name}.order_status_log"
bronze_orders_tbl = f"{bronze_catalog}.{schema_name}.orders"
silver_log_tbl = f"{silver_catalog}.{schema_name}.order_status_log"
quarantine_log_tbl = f"{quarantine_catalog}.{schema_name}.order_status_log"

print(f"pipeline_run_id    = {pipeline_run_id}")
print(f"bronze_log_tbl     = {bronze_log_tbl}")
print(f"silver_log_tbl     = {silver_log_tbl}")
print(f"quarantine_log_tbl = {quarantine_log_tbl}")

VALID_STATUSES = [
    "PENDING", "PROCESSING", "SHIPPED", "DELIVERED",
    "CANCELLED", "RETURNED",
    "RETURN_INITIATED",  # transitional state per generator/status_updates.py
]

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze + dedup on `log_id`

# COMMAND ----------

df_bronze = spark.table(bronze_log_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_log_tbl}")

dedup_window = Window.partitionBy("log_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)

dedup_count = df_dedup.count()
print(f"After dedup on log_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. FK reference set — known order_ids in bronze.orders

# COMMAND ----------

df_known_orders = (
    spark.table(bronze_orders_tbl)
        .select("order_id")
        .distinct()
)
known_order_count = df_known_orders.count()
print(f"Reference set: {known_order_count:,} distinct order_ids in {bronze_orders_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Tag each row with DQ flags

# COMMAND ----------

df_with_fk = (
    df_dedup.alias("l")
        .join(
            df_known_orders.alias("o").withColumn("_order_known", F.lit(True)),
            on="order_id",
            how="left",
        )
        .withColumn("_order_known", F.coalesce(F.col("_order_known"), F.lit(False)))
)

valid_status_arr = F.array(*[F.lit(s) for s in VALID_STATUSES])

# Status validity:
# - to_status (NOT NULL'd at DQ level) must be in valid set
# - from_status, if not null, must be in valid set
# Either failure → single INVALID_STATUS code (per SCHEMA.md — combined rule)
df_dq = (
    df_with_fk
        .withColumn("_v_null_log_id",     F.when(F.col("log_id").isNull(),                                  F.lit("NULL_LOG_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_order_id",   F.when(F.col("order_id").isNull(),                                F.lit("NULL_ORDER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_to_status",  F.when(F.col("to_status").isNull(),                               F.lit("NULL_TO_STATUS")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_order",   F.when(F.col("order_id").isNotNull() & (~F.col("_order_known")),  F.lit("UNKNOWN_ORDER_ID")).otherwise(F.lit(None)))
        .withColumn(
            "_v_invalid_status",
            F.when(
                (F.col("to_status").isNotNull() & ~F.array_contains(valid_status_arr, F.col("to_status")))
                | (F.col("from_status").isNotNull() & ~F.array_contains(valid_status_arr, F.col("from_status"))),
                F.lit("INVALID_STATUS"),
            ).otherwise(F.lit(None)),
        )
)

violation_cols = [
    "_v_null_log_id",
    "_v_null_order_id",
    "_v_null_to_status",
    "_v_unknown_order",
    "_v_invalid_status",
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
        .drop("_violations_array", "_order_known", *violation_cols)
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
            F.col("log_id"),
            F.col("order_id"),
            F.col("from_status"),
            F.col("to_status"),
            F.col("changed_at"),
            F.col("changed_by"),
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

silver_table_exists = spark.catalog.tableExists(silver_log_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_log_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_log_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.order_status_log`

# COMMAND ----------

staging_view = "silver_order_status_log_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_log_tbl} AS tgt
USING {staging_view} AS src
ON tgt.log_id = src.log_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.order_status_log`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("log_id"), F.col("order_id"),
                F.col("from_status"), F.col("to_status"),
                F.col("changed_at"), F.col("changed_by"),
                F.col("_source_file"),
            )))
            .withColumn("source_table", F.lit("velora_oms.order_status_log"))
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
            .saveAsTable(quarantine_log_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_log_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_log_tbl).count()
print(f"{silver_log_tbl} now holds {silver_total:,} rows total")

print("\nTransition counts (top 10):")
display(
    spark.table(silver_log_tbl)
        .groupBy("from_status", "to_status")
        .agg(F.count("*").alias("n"))
        .orderBy(F.col("n").desc())
        .limit(10)
)

quarantine_total = (
    spark.table(quarantine_log_tbl).count()
    if spark.catalog.tableExists(quarantine_log_tbl) else 0
)
print(f"{quarantine_log_tbl} now holds {quarantine_total:,} rows total")
