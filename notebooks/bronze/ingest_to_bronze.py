# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze layer — landing → bronze.{entity}
# MAGIC
# MAGIC Reads Parquet from `landing/{entity}/(date=*|full)/`, appends 4 standard
# MAGIC audit columns, writes append-only to `bronze.default.{entity}` Delta.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `entity_name` | `orders` | yes |
# MAGIC | `pipeline_run_id` | uuid | yes — ADF passes this; fall back to a random uuid for manual runs |
# MAGIC | `landing_account` | `pipelineiqadlsdev` | no — default set |
# MAGIC | `bronze_catalog` | `bronze` | no — default set |
# MAGIC
# MAGIC ## Medallion contract (per CLAUDE.md)
# MAGIC
# MAGIC - Bronze is **append-only**. No dedup. No business logic.
# MAGIC - Schema is enforced from the source Parquet exactly as-is.
# MAGIC - 4 audit columns added: `_source_file`, `_ingestion_timestamp`, `_pipeline_run_id`, `_bronze_timestamp`.
# MAGIC - Data flows landing → bronze only. Bronze never reads silver/gold.

# COMMAND ----------

import uuid
from pyspark.sql import functions as F

# COMMAND ----------

dbutils.widgets.text("entity_name", "orders")
dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("landing_account", "pipelineiqadlsdev")
dbutils.widgets.text("bronze_catalog", "bronze")
dbutils.widgets.text("bronze_schema", "default")

entity_name = dbutils.widgets.get("entity_name").strip()
pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
landing_account = dbutils.widgets.get("landing_account").strip()
bronze_catalog = dbutils.widgets.get("bronze_catalog").strip()
bronze_schema = dbutils.widgets.get("bronze_schema").strip()

if not entity_name:
    raise ValueError("entity_name is required")

print(f"entity_name      = {entity_name}")
print(f"pipeline_run_id  = {pipeline_run_id}")
print(f"landing_account  = {landing_account}")
print(f"target_table     = {bronze_catalog}.{bronze_schema}.{entity_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read all Parquet under `landing/{entity}/`
# MAGIC
# MAGIC `recursiveFileLookup` lets us read both `date=2026-01-15/*.parquet` and
# MAGIC `full/*.parquet` patterns under one logical path — the by-date and full
# MAGIC modes from the export script (`scripts/export_velora_to_landing.py`)
# MAGIC unify here.

# COMMAND ----------

landing_path = f"abfss://landing@{landing_account}.dfs.core.windows.net/{entity_name}/"

df_landing = (
    spark.read
        .option("recursiveFileLookup", "true")
        .parquet(landing_path)
)

source_count = df_landing.count()
print(f"Read {source_count:,} rows from {landing_path}")

if source_count == 0:
    raise RuntimeError(
        f"No rows read from {landing_path} — has the export script run for "
        f"{entity_name}? Check `scripts/export_velora_to_landing.py` output."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Append the 4 audit columns
# MAGIC
# MAGIC `_source_file` is set per-row from the actual file each row came from
# MAGIC (Spark's `input_file_name()`). The other 3 are constants for this run.

# COMMAND ----------

df_bronze = (
    df_landing
        .withColumn("_source_file", F.input_file_name())
        .withColumn("_ingestion_timestamp", F.current_timestamp())
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_bronze_timestamp", F.current_timestamp())
        .withColumn("_ingestion_date", F.to_date(F.col("_ingestion_timestamp")))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Ensure the target schema exists, then append
# MAGIC
# MAGIC Schema creation is idempotent. The first run of any Bronze notebook
# MAGIC creates `bronze.default` once; subsequent runs no-op.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{bronze_catalog}`.`{bronze_schema}`")

target = f"`{bronze_catalog}`.`{bronze_schema}`.`{entity_name}`"

# Partition Bronze by ingestion date for cheap time-window pruning later.
(
    df_bronze.write
        .format("delta")
        .mode("append")
        .partitionBy("_ingestion_date")
        .option("mergeSchema", "true")
        .saveAsTable(target.replace("`", ""))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Verify

# COMMAND ----------

bronze_count = spark.table(target.replace("`", "")).count()
print(f"{target} now holds {bronze_count:,} rows total.")

display(
    spark.table(target.replace("`", ""))
        .orderBy(F.col("_bronze_timestamp").desc())
        .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern notes
# MAGIC
# MAGIC - This notebook is **entity-agnostic**. All 10 source entities use the
# MAGIC   same code path; only `entity_name` widget changes per run.
# MAGIC - ADF (Tier 6) will eventually invoke this notebook 10× per pipeline run,
# MAGIC   one per entity, with `pipeline_run_id` set to the ADF run ID.
# MAGIC - Schema drift is intentionally tolerated (`mergeSchema = true`) because
# MAGIC   Bronze is "as it landed" — Silver is where validation happens.
