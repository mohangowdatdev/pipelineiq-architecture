# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — synthesized gold.dim_territory (SCD Type 1)
# MAGIC
# MAGIC `dim_territory` has no source table — it's synthesized in this
# MAGIC notebook from:
# MAGIC
# MAGIC 1. **Distinct `territory_id`s** observed in `gold.dim_store` +
# MAGIC    `silver.territory_assignments` (the two natural origins of
# MAGIC    territory codes in the warehouse).
# MAGIC 2. **A hardcoded enrichment lookup** (`TERRITORY_ENRICHMENT` below)
# MAGIC    mapping each `territory_id` → `(territory_name, city, state, region)`.
# MAGIC    Values match `generator/config.py::CITIES`.
# MAGIC 3. **One sentinel row, `D2C_NATIONAL`** (DECISIONS #55) — D2C orders
# MAGIC    have no store and no rep, so no natural territory; the sentinel
# MAGIC    keeps `fact_order_line.territory_id` NOT NULL and lets every
# MAGIC    territory aggregation sum cleanly without filtering.
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC MERGE on `territory_id`. SCD Type 1 — re-runs UPSERT in place.
# MAGIC
# MAGIC ## Coverage check
# MAGIC
# MAGIC The notebook prints any observed `territory_id` that's missing from
# MAGIC `TERRITORY_ENRICHMENT`. Such rows still get written (with NULL
# MAGIC city/state and `region = 'UNKNOWN'`) so dashboards don't drop them,
# MAGIC but the missing-keys list is the prompt to extend the lookup.

# COMMAND ----------

import uuid
from pyspark.sql import Row, functions as F

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("silver_catalog", "silver")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
silver_catalog = dbutils.widgets.get("silver_catalog").strip()
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

silver_assignments_tbl = f"{silver_catalog}.{schema_name}.territory_assignments"
dim_store_tbl = f"{gold_catalog}.{schema_name}.dim_store"
dim_territory_tbl = f"{gold_catalog}.{schema_name}.dim_territory"

print(f"pipeline_run_id         = {pipeline_run_id}")
print(f"silver_assignments_tbl  = {silver_assignments_tbl}")
print(f"dim_store_tbl           = {dim_store_tbl}")
print(f"dim_territory_tbl       = {dim_territory_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Hardcoded enrichment lookup
# MAGIC
# MAGIC Mirrors `generator/config.py::CITIES`. `region` uppercased for
# MAGIC warehouse consistency (matches `'NATIONAL'` sentinel convention in
# MAGIC SCHEMA.md).

# COMMAND ----------

# territory_id → (territory_name, city, state, region)
TERRITORY_ENRICHMENT = {
    "TER-001": ("South 1", "Bangalore", "Karnataka",   "SOUTH"),
    "TER-002": ("South 2", "Chennai",   "Tamil Nadu",  "SOUTH"),
    "TER-003": ("South 3", "Hyderabad", "Telangana",   "SOUTH"),
    "TER-004": ("West 1",  "Mumbai",    "Maharashtra", "WEST"),
    "TER-005": ("West 2",  "Pune",      "Maharashtra", "WEST"),
    "TER-006": ("North 1", "Delhi",     "Delhi",       "NORTH"),
    "TER-007": ("East 1",  "Kolkata",   "West Bengal", "EAST"),
    "TER-008": ("West 3",  "Ahmedabad", "Gujarat",     "WEST"),
}

SENTINEL = Row(
    territory_id="D2C_NATIONAL",
    territory_name="D2C National",
    city=None,
    state=None,
    region="NATIONAL",
    is_active=True,
)

print(f"Enrichment lookup covers {len(TERRITORY_ENRICHMENT)} territory_ids")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Collect observed territory_ids from dim_store + silver.territory_assignments

# COMMAND ----------

df_observed_from_stores = (
    spark.table(dim_store_tbl)
        .select("territory_id")
        .filter(F.col("territory_id").isNotNull())
        .distinct()
)
df_observed_from_assignments = (
    spark.table(silver_assignments_tbl)
        .filter(F.col("dq_passed") == True)
        .select("territory_id")
        .filter(F.col("territory_id").isNotNull())
        .distinct()
)

df_observed = df_observed_from_stores.unionByName(df_observed_from_assignments).distinct()
observed_ids = sorted(r["territory_id"] for r in df_observed.collect())
print(f"Observed {len(observed_ids)} distinct territory_ids: {observed_ids}")

missing_keys = [tid for tid in observed_ids if tid not in TERRITORY_ENRICHMENT]
if missing_keys:
    print(f"  WARN: {len(missing_keys)} territory_ids missing from TERRITORY_ENRICHMENT — "
          f"will be written with NULL city/state and region='UNKNOWN':")
    for tid in missing_keys:
        print(f"    - {tid}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Build dim rows: enriched observed + sentinel

# COMMAND ----------

dim_rows = []
for tid in observed_ids:
    if tid in TERRITORY_ENRICHMENT:
        name, city, state, region = TERRITORY_ENRICHMENT[tid]
    else:
        name, city, state, region = (tid, None, None, "UNKNOWN")
    dim_rows.append(Row(
        territory_id=tid,
        territory_name=name,
        city=city,
        state=state,
        region=region,
        is_active=True,
    ))

# Sentinel always appended (only if not already present, since assignments
# could in principle introduce 'D2C_NATIONAL' explicitly later — guard
# anyway).
if SENTINEL.territory_id not in observed_ids:
    dim_rows.append(SENTINEL)

print(f"Building {len(dim_rows)} dim rows (including D2C_NATIONAL sentinel)")

df_dim_batch = (
    spark.createDataFrame(dim_rows)
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            "territory_id",
            "territory_name",
            "city",
            "state",
            "region",
            "is_active",
            "_pipeline_run_id",
            "_gold_timestamp",
        )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Ensure table + MERGE on territory_id

# COMMAND ----------

dim_exists = spark.catalog.tableExists(dim_territory_tbl)
print(f"{dim_territory_tbl} exists: {dim_exists}")

if not dim_exists:
    print(f"Creating {dim_territory_tbl} for the first time")
    df_dim_batch.limit(0).write.format("delta").saveAsTable(dim_territory_tbl)

staging_view = "dim_territory_staging"
df_dim_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {dim_territory_tbl} AS tgt
USING {staging_view} AS src
ON tgt.territory_id = src.territory_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""
print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Verify

# COMMAND ----------

dim_total = spark.table(dim_territory_tbl).count()
sentinel_present = (
    spark.table(dim_territory_tbl)
        .filter(F.col("territory_id") == "D2C_NATIONAL")
        .count() == 1
)
distinct_pks = spark.table(dim_territory_tbl).select("territory_id").distinct().count()

print(f"{dim_territory_tbl} total rows:     {dim_total:,}")
print(f"  distinct territory_id values:     {distinct_pks:,}  ({'OK' if distinct_pks == dim_total else 'DUPLICATE PK'})")
print(f"  D2C_NATIONAL sentinel present:    {sentinel_present}")

display(
    spark.table(dim_territory_tbl)
        .orderBy("region", "territory_id")
)
