# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.sales_reps → silver.sales_reps + quarantine.sales_reps
# MAGIC
# MAGIC Reads `bronze.default.sales_reps` (append-only history), dedups by
# MAGIC `rep_id` keeping the latest `_bronze_timestamp`, runs DQ rules, then
# MAGIC MERGEs into `silver.default.sales_reps`. Rejected rows are appended to
# MAGIC `quarantine.default.sales_reps`.
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
# MAGIC - Silver is **deduplicated** on the business key (`rep_id`).
# MAGIC - Silver is DQ-validated; bad rows are quarantined.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - **No SCD tracking** on this table — the only SCD-2 attr in
# MAGIC   `gold.dim_sales_rep` is `territory_id`, which is sourced from
# MAGIC   `silver.territory_assignments`.
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `rep_id` IS NULL | `NULL_REP_ID` |
# MAGIC | `email` IS NULL OR not LIKE `%@%` | `INVALID_EMAIL` |
# MAGIC | `full_name` IS NULL | `NULL_FULL_NAME` |
# MAGIC | `hire_date` IS NULL | `NULL_HIRE_DATE` |

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

bronze_reps_tbl = f"{bronze_catalog}.{schema_name}.sales_reps"
silver_reps_tbl = f"{silver_catalog}.{schema_name}.sales_reps"
quarantine_reps_tbl = f"{quarantine_catalog}.{schema_name}.sales_reps"

print(f"pipeline_run_id      = {pipeline_run_id}")
print(f"bronze_reps_tbl      = {bronze_reps_tbl}")
print(f"silver_reps_tbl      = {silver_reps_tbl}")
print(f"quarantine_reps_tbl  = {quarantine_reps_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze sales_reps + dedup on `rep_id`

# COMMAND ----------

df_bronze = spark.table(bronze_reps_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_reps_tbl}")

dedup_window = Window.partitionBy("rep_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)
dedup_count = df_dedup.count()
print(f"After dedup on rep_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Tag each row with DQ flags

# COMMAND ----------

df_dq = (
    df_dedup
        .withColumn("_v_null_rep_id",     F.when(F.col("rep_id").isNull(),                                F.lit("NULL_REP_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_email",   F.when(F.col("email").isNull() | ~F.col("email").contains("@"), F.lit("INVALID_EMAIL")).otherwise(F.lit(None)))
        .withColumn("_v_null_full_name",  F.when(F.col("full_name").isNull(),                             F.lit("NULL_FULL_NAME")).otherwise(F.lit(None)))
        .withColumn("_v_null_hire_date",  F.when(F.col("hire_date").isNull(),                             F.lit("NULL_HIRE_DATE")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_rep_id",
    "_v_invalid_email",
    "_v_null_full_name",
    "_v_null_hire_date",
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
            F.col("rep_id"),
            F.col("full_name"),
            F.col("email"),
            F.col("phone"),
            F.col("hire_date"),
            F.col("is_active"),
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

silver_table_exists = spark.catalog.tableExists(silver_reps_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_reps_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_reps_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE into `silver.sales_reps`

# COMMAND ----------

staging_view = "silver_sales_reps_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_reps_tbl} AS tgt
USING {staging_view} AS src
ON tgt.rep_id = src.rep_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Append rejected rows to `quarantine.sales_reps`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("rep_id"), F.col("full_name"), F.col("email"),
                F.col("phone"), F.col("hire_date"), F.col("is_active"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_hrm.sales_reps"))
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
            .saveAsTable(quarantine_reps_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_reps_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Verify

# COMMAND ----------

silver_total = spark.table(silver_reps_tbl).count()
print(f"{silver_reps_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_reps_tbl).count()
    if spark.catalog.tableExists(quarantine_reps_tbl) else 0
)
print(f"{quarantine_reps_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_reps_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
