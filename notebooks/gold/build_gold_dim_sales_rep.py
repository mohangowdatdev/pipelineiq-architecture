# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — silver.sales_reps + silver.territory_assignments → gold.dim_sales_rep (SCD Type 2)
# MAGIC
# MAGIC SCD Type 2 on `territory_id`. SCD Type 1 on every other attr
# MAGIC (full_name, email, is_active).
# MAGIC
# MAGIC ## Source pattern (per DECISIONS #57 + SCHEMA.md)
# MAGIC
# MAGIC `silver.territory_assignments` is the source-side SCD-2 timeline —
# MAGIC each row is one assignment period for a rep. We project assignment
# MAGIC rows directly:
# MAGIC
# MAGIC | Field | Comes from |
# MAGIC |---|---|
# MAGIC | `valid_from` | `silver.territory_assignments.assigned_from` |
# MAGIC | `valid_to` | `LEAD(assigned_from) OVER (PARTITION BY rep_id ORDER BY assigned_from) - 1` (NULL for the latest row) |
# MAGIC | `is_current` | `valid_to IS NULL` |
# MAGIC | `territory_id` | from the assignment row itself |
# MAGIC | `full_name`, `email`, `is_active` | latest values from `silver.sales_reps` |
# MAGIC | `surrogate_key` | `xxhash64(rep_id, valid_from)` (deterministic, stateless) |
# MAGIC
# MAGIC `valid_to` is computed via window (LEAD), not from
# MAGIC `territory_assignments.assigned_to`, so the dim is self-consistent
# MAGIC regardless of source bookkeeping.
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC `MERGE ON surrogate_key WHEN MATCHED UPDATE SET *` handles both:
# MAGIC - existing version with same `valid_from` → SCD-1 attrs refresh, plus
# MAGIC   `valid_to`/`is_current` flip when a new assignment supersedes it
# MAGIC - new assignment row → INSERT new dim version (new surrogate key)

# COMMAND ----------

import uuid
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("silver_catalog", "silver")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
silver_catalog = dbutils.widgets.get("silver_catalog").strip()
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

silver_reps_tbl = f"{silver_catalog}.{schema_name}.sales_reps"
silver_assignments_tbl = f"{silver_catalog}.{schema_name}.territory_assignments"
dim_sales_rep_tbl = f"{gold_catalog}.{schema_name}.dim_sales_rep"

print(f"pipeline_run_id         = {pipeline_run_id}")
print(f"silver_reps_tbl         = {silver_reps_tbl}")
print(f"silver_assignments_tbl  = {silver_assignments_tbl}")
print(f"dim_sales_rep_tbl       = {dim_sales_rep_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Silver inputs (DQ-passed only)

# COMMAND ----------

df_reps = (
    spark.table(silver_reps_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "rep_id",
            "full_name",
            "email",
            "is_active",
        )
)
reps_count = df_reps.count()
print(f"silver.sales_reps clean rows:             {reps_count:,}")

df_assignments = (
    spark.table(silver_assignments_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "assignment_id",
            "rep_id",
            "territory_id",
            "assigned_from",
        )
)
assignments_count = df_assignments.count()
print(f"silver.territory_assignments clean rows:  {assignments_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Compute SCD-2 versions from assignment timeline

# COMMAND ----------

w_rep_timeline = Window.partitionBy("rep_id").orderBy("assigned_from")

df_versioned = (
    df_assignments
        .withColumn("valid_from", F.col("assigned_from"))
        .withColumn(
            "_next_assigned_from",
            F.lead("assigned_from").over(w_rep_timeline),
        )
        .withColumn(
            "valid_to",
            F.when(
                F.col("_next_assigned_from").isNotNull(),
                F.date_sub(F.col("_next_assigned_from"), 1),
            ).otherwise(F.lit(None).cast("date")),
        )
        .withColumn("is_current", F.col("valid_to").isNull())
        .drop("_next_assigned_from", "assigned_from")
)

versions_count = df_versioned.count()
current_versions = df_versioned.filter(F.col("is_current")).count()
print(f"Computed {versions_count:,} dim versions ({current_versions:,} currently active)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Join with current rep attrs (SCD-1)

# COMMAND ----------

df_dim_batch = (
    df_versioned.alias("ta")
        .join(df_reps.alias("r"), on="rep_id", how="inner")
        .withColumn(
            "surrogate_key",
            F.xxhash64(F.col("rep_id"), F.col("valid_from")),
        )
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            "surrogate_key",
            "rep_id",
            F.col("r.full_name").alias("full_name"),
            F.col("r.email").alias("email"),
            "territory_id",
            F.col("r.is_active").alias("is_active"),
            "valid_from",
            "valid_to",
            "is_current",
            "_pipeline_run_id",
            "_gold_timestamp",
        )
)

batch_count = df_dim_batch.count()
print(f"Dim batch rows ready to MERGE: {batch_count:,}")
if batch_count != versions_count:
    orphaned = versions_count - batch_count
    print(f"  WARN: {orphaned:,} assignment rows have no matching silver.sales_reps row (FK mismatch)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Ensure table + MERGE on surrogate_key

# COMMAND ----------

dim_exists = spark.catalog.tableExists(dim_sales_rep_tbl)
print(f"{dim_sales_rep_tbl} exists: {dim_exists}")

if not dim_exists:
    print(f"Creating {dim_sales_rep_tbl} for the first time")
    (
        df_dim_batch.limit(0).write
            .format("delta")
            .partitionBy("is_current")
            .saveAsTable(dim_sales_rep_tbl)
    )

staging_view = "dim_sales_rep_staging"
df_dim_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {dim_sales_rep_tbl} AS tgt
USING {staging_view} AS src
ON tgt.surrogate_key = src.surrogate_key
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""
print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Verify

# COMMAND ----------

dim_total = spark.table(dim_sales_rep_tbl).count()
dim_current_total = spark.table(dim_sales_rep_tbl).filter(F.col("is_current") == True).count()
distinct_nks = spark.table(dim_sales_rep_tbl).select("rep_id").distinct().count()

print(f"{dim_sales_rep_tbl} total rows:    {dim_total:,}")
print(f"  is_current = true:                {dim_current_total:,}")
print(f"  distinct rep_id values:           {distinct_nks:,}")
print(f"  expected (current rows == NKs):   {dim_current_total == distinct_nks}")

surrogate_unique = spark.table(dim_sales_rep_tbl).select("surrogate_key").distinct().count()
print(f"  distinct surrogate_keys:          {surrogate_unique:,}  ({'OK' if surrogate_unique == dim_total else 'COLLISION'})")

display(
    spark.table(dim_sales_rep_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)
