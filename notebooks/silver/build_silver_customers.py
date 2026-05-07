# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.customers → silver.customers + quarantine.customers
# MAGIC
# MAGIC Reads `bronze.default.customers` (append-only history), dedups by
# MAGIC `customer_id` keeping the latest row per business key, runs DQ rules,
# MAGIC and computes SCD-change-tracking columns (`_prev_segment`, `_prev_city`,
# MAGIC `_scd_changed`) by comparing each incoming row against the current state
# MAGIC of `silver.default.customers`. The tracking columns drive Gold's SCD
# MAGIC Type 2 logic in `gold.dim_customer` (DECISIONS #15).
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
# MAGIC - Silver is **deduplicated** on the business key (`customer_id`).
# MAGIC - Silver is **DQ-validated** — each row carries `dq_passed` + `dq_rejection_reason`.
# MAGIC - Bad rows are quarantined, never silently dropped.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - SCD-change tracking is computed at Silver, applied at Gold.
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `customer_id` IS NULL | `NULL_CUSTOMER_ID` |
# MAGIC | `email` IS NULL OR not LIKE `%@%` | `INVALID_EMAIL` |
# MAGIC | `segment` not in (`INDIVIDUAL`,`BUSINESS`,`VIP`) | `INVALID_SEGMENT` |
# MAGIC | `account_type` not in (`D2C`,`B2B`) | `INVALID_ACCOUNT_TYPE` |
# MAGIC | `full_name` IS NULL | `NULL_FULL_NAME` |
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

bronze_customers_tbl = f"{bronze_catalog}.{schema_name}.customers"
silver_customers_tbl = f"{silver_catalog}.{schema_name}.customers"
quarantine_customers_tbl = f"{quarantine_catalog}.{schema_name}.customers"

print(f"pipeline_run_id          = {pipeline_run_id}")
print(f"bronze_customers_tbl     = {bronze_customers_tbl}")
print(f"silver_customers_tbl     = {silver_customers_tbl}")
print(f"quarantine_customers_tbl = {quarantine_customers_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze customers + dedup on `customer_id`

# COMMAND ----------

df_bronze = spark.table(bronze_customers_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_customers_tbl}")

dedup_window = Window.partitionBy("customer_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)
dedup_count = df_dedup.count()
print(f"After dedup on customer_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Tag each row with DQ flags

# COMMAND ----------

VALID_SEGMENTS = ["INDIVIDUAL", "BUSINESS", "VIP"]
VALID_ACCOUNT_TYPES = ["D2C", "B2B"]

df_dq = (
    df_dedup
        .withColumn("_v_null_customer_id", F.when(F.col("customer_id").isNull(),                                    F.lit("NULL_CUSTOMER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_email",    F.when(F.col("email").isNull() | ~F.col("email").contains("@"),          F.lit("INVALID_EMAIL")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_segment",  F.when(~F.col("segment").isin(VALID_SEGMENTS),                           F.lit("INVALID_SEGMENT")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_account",  F.when(~F.col("account_type").isin(VALID_ACCOUNT_TYPES),                 F.lit("INVALID_ACCOUNT_TYPE")).otherwise(F.lit(None)))
        .withColumn("_v_null_full_name",   F.when(F.col("full_name").isNull(),                                      F.lit("NULL_FULL_NAME")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_customer_id",
    "_v_invalid_email",
    "_v_invalid_segment",
    "_v_invalid_account",
    "_v_null_full_name",
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
# MAGIC ## 3. Compute SCD-change tracking
# MAGIC
# MAGIC For SCD Type 2 in Gold, we need to know which incoming rows changed
# MAGIC `segment` or `city` vs. the current Silver state. We **left-join the
# MAGIC clean batch against the existing `silver.default.customers`** (key only)
# MAGIC and compare. First-time customers have no prior state and get
# MAGIC `_scd_changed = false` (no SCD event — they're new); existing customers
# MAGIC whose tracked attributes changed get `_scd_changed = true` plus the
# MAGIC prior values in `_prev_segment` / `_prev_city`.

# COMMAND ----------

silver_table_exists = spark.catalog.tableExists(silver_customers_tbl)

if silver_table_exists:
    df_current = (
        spark.table(silver_customers_tbl)
            .select(
                F.col("customer_id").alias("_curr_customer_id"),
                F.col("segment").alias("_curr_segment"),
                F.col("city").alias("_curr_city"),
            )
    )
else:
    print(f"{silver_customers_tbl} does not exist — first run, no prior state.")
    df_current = spark.createDataFrame(
        [], "_curr_customer_id STRING, _curr_segment STRING, _curr_city STRING"
    )

df_with_scd = (
    df_tagged.filter(F.col("dq_passed")).alias("incoming")
        .join(df_current.alias("curr"),
              F.col("incoming.customer_id") == F.col("curr._curr_customer_id"),
              "left")
        .withColumn("_prev_segment", F.col("_curr_segment"))
        .withColumn("_prev_city",    F.col("_curr_city"))
        .withColumn(
            "_scd_changed",
            F.when(
                F.col("_curr_customer_id").isNull(), F.lit(False)
            ).otherwise(
                (F.col("segment") != F.col("_curr_segment")) |
                (F.col("city")    != F.col("_curr_city"))
            ),
        )
        .drop("_curr_customer_id", "_curr_segment", "_curr_city")
)

scd_changed_count = df_with_scd.filter(F.col("_scd_changed")).count()
new_customer_count = df_with_scd.filter(F.col("_prev_segment").isNull()).count()
print(f"SCD changes detected: {scd_changed_count:,}")
print(f"New customers (no prior state): {new_customer_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Project to the Silver schema

# COMMAND ----------

df_silver_batch = (
    df_with_scd
        .withColumn("_silver_timestamp", F.current_timestamp())
        .select(
            F.col("customer_id"),
            F.col("full_name"),
            F.col("email"),
            F.col("segment"),
            F.col("city"),
            F.col("state"),
            F.col("account_type"),
            F.col("is_active"),
            F.col("_prev_segment"),
            F.col("_prev_city"),
            F.col("_scd_changed"),
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
# MAGIC ## 5. Ensure schemas + create empty Silver table on first run

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{silver_catalog}`.`{schema_name}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{quarantine_catalog}`.`{schema_name}`")

if not silver_table_exists:
    print(f"Creating empty {silver_customers_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_customers_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.customers`
# MAGIC
# MAGIC `MERGE` on `customer_id`. Existing row updates overwrite the prior values
# MAGIC (dimension is mutable at source per SCHEMA.md). The `_prev_*` columns on
# MAGIC the *new* version capture the about-to-be-overwritten state — Gold reads
# MAGIC those to close the prior SCD-2 row before inserting the new one.

# COMMAND ----------

staging_view = "silver_customers_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_customers_tbl} AS tgt
USING {staging_view} AS src
ON tgt.customer_id = src.customer_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.customers`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("customer_id"), F.col("full_name"), F.col("email"),
                F.col("segment"), F.col("city"), F.col("state"),
                F.col("account_type"), F.col("is_active"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_crm.customers"))
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
            .saveAsTable(quarantine_customers_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_customers_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_customers_tbl).count()
print(f"{silver_customers_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_customers_tbl).count()
    if spark.catalog.tableExists(quarantine_customers_tbl) else 0
)
print(f"{quarantine_customers_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_customers_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern notes
# MAGIC
# MAGIC - SCD-change tracking is computed **at Silver** by joining incoming rows
# MAGIC   against current Silver state. Gold's `dim_customer` notebook reads
# MAGIC   `_scd_changed` + `_prev_*` and applies the close-old / insert-new
# MAGIC   pattern; the comparison logic lives here, the SCD state machine
# MAGIC   lives there.
# MAGIC - First-time customers always have `_scd_changed = false` — they're
# MAGIC   not an SCD *event*, they're an initial dimension row.
# MAGIC - Tracked SCD attributes for `dim_customer` are `segment` + `city`
# MAGIC   (per DECISIONS #15). Adding more SCD-tracked attrs requires a change
# MAGIC   here AND in `gold.dim_customer` — keep them in sync.
