# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.territory_assignments → silver.territory_assignments + quarantine
# MAGIC
# MAGIC Reads `bronze.default.territory_assignments` (append-only assignment log),
# MAGIC dedups by `assignment_id` keeping the latest `_bronze_timestamp`, runs DQ
# MAGIC rules, then MERGEs into `silver.default.territory_assignments`. Rejected
# MAGIC rows are appended to `quarantine.default.territory_assignments`.
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
# MAGIC - Silver is **deduplicated** on the business key (`assignment_id`).
# MAGIC - Silver is DQ-validated; bad rows are quarantined.
# MAGIC - MERGE on business key — re-runs are idempotent. (Source is append-only,
# MAGIC   but `assigned_to` flips from NULL to a date when an assignment ends, so
# MAGIC   we still MERGE rather than INSERT.)
# MAGIC - Drives `gold.dim_sales_rep` SCD-2 territory tracking.
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `assignment_id` IS NULL | `NULL_ASSIGNMENT_ID` |
# MAGIC | `rep_id` IS NULL | `NULL_REP_ID` |
# MAGIC | `territory_id` IS NULL | `NULL_TERRITORY_ID` |
# MAGIC | `rep_id` not in `bronze.sales_reps` | `UNKNOWN_REP_ID` |
# MAGIC | `assigned_from` IS NULL | `NULL_ASSIGNED_FROM` |

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

bronze_assign_tbl = f"{bronze_catalog}.{schema_name}.territory_assignments"
bronze_reps_tbl = f"{bronze_catalog}.{schema_name}.sales_reps"
silver_assign_tbl = f"{silver_catalog}.{schema_name}.territory_assignments"
quarantine_assign_tbl = f"{quarantine_catalog}.{schema_name}.territory_assignments"

print(f"pipeline_run_id       = {pipeline_run_id}")
print(f"bronze_assign_tbl     = {bronze_assign_tbl}")
print(f"bronze_reps_tbl       = {bronze_reps_tbl}")
print(f"silver_assign_tbl     = {silver_assign_tbl}")
print(f"quarantine_assign_tbl = {quarantine_assign_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze territory_assignments + dedup on `assignment_id`

# COMMAND ----------

df_bronze = spark.table(bronze_assign_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_assign_tbl}")

dedup_window = Window.partitionBy("assignment_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)
dedup_count = df_dedup.count()
print(f"After dedup on assignment_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build rep-id reference set for FK validation

# COMMAND ----------

df_known_reps = (
    spark.table(bronze_reps_tbl)
        .select("rep_id")
        .distinct()
)
known_rep_count = df_known_reps.count()
print(f"Reference set: {known_rep_count:,} distinct rep_ids in {bronze_reps_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Tag each row with DQ flags

# COMMAND ----------

df_with_fk = (
    df_dedup.alias("ta")
        .join(
            df_known_reps.alias("r").withColumn("_rep_known", F.lit(True)),
            on="rep_id",
            how="left",
        )
        .withColumn("_rep_known", F.coalesce(F.col("_rep_known"), F.lit(False)))
)

df_dq = (
    df_with_fk
        .withColumn("_v_null_assignment_id", F.when(F.col("assignment_id").isNull(),                          F.lit("NULL_ASSIGNMENT_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_rep_id",        F.when(F.col("rep_id").isNull(),                                 F.lit("NULL_REP_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_territory_id",  F.when(F.col("territory_id").isNull(),                           F.lit("NULL_TERRITORY_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_rep",        F.when(F.col("rep_id").isNotNull() & (~F.col("_rep_known")),     F.lit("UNKNOWN_REP_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_assigned_from", F.when(F.col("assigned_from").isNull(),                          F.lit("NULL_ASSIGNED_FROM")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_assignment_id",
    "_v_null_rep_id",
    "_v_null_territory_id",
    "_v_unknown_rep",
    "_v_null_assigned_from",
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
        .drop("_violations_array", "_rep_known", *violation_cols)
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
            F.col("assignment_id"),
            F.col("rep_id"),
            F.col("territory_id"),
            F.col("assigned_from"),
            F.col("assigned_to"),
            F.col("is_current"),
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

silver_table_exists = spark.catalog.tableExists(silver_assign_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_assign_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_assign_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.territory_assignments`

# COMMAND ----------

staging_view = "silver_territory_assignments_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_assign_tbl} AS tgt
USING {staging_view} AS src
ON tgt.assignment_id = src.assignment_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.territory_assignments`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("assignment_id"), F.col("rep_id"), F.col("territory_id"),
                F.col("assigned_from"), F.col("assigned_to"), F.col("is_current"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_hrm.territory_assignments"))
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
            .saveAsTable(quarantine_assign_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_assign_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_assign_tbl).count()
print(f"{silver_assign_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_assign_tbl).count()
    if spark.catalog.tableExists(quarantine_assign_tbl) else 0
)
print(f"{quarantine_assign_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_assign_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
