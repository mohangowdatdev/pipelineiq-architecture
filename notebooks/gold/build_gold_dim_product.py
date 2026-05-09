# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — silver.products + silver.product_pricing → gold.dim_product (SCD Type 2)
# MAGIC
# MAGIC SCD Type 2 on `list_price`. SCD Type 1 on every other attr (sku,
# MAGIC product_name, division, brand, category_id, is_active, cost_price,
# MAGIC pricing_type).
# MAGIC
# MAGIC ## Source pattern (per DECISIONS #57 + SCHEMA.md)
# MAGIC
# MAGIC The source's `silver.product_pricing` *is* the SCD-2 timeline — each
# MAGIC row is one price-effective period. We don't synthesise versions; we
# MAGIC project pricing rows directly:
# MAGIC
# MAGIC | Field | Comes from |
# MAGIC |---|---|
# MAGIC | `valid_from` | `silver.product_pricing.effective_from` |
# MAGIC | `valid_to` | `LEAD(effective_from) OVER (PARTITION BY product_id ORDER BY effective_from) - 1` (NULL for the latest row) |
# MAGIC | `is_current` | `valid_to IS NULL` |
# MAGIC | `list_price`, `cost_price`, `pricing_type` | from the pricing row itself |
# MAGIC | `sku`, `product_name`, `division`, `brand`, `category_id`, `is_active` | latest values from `silver.products` |
# MAGIC | `surrogate_key` | `xxhash64(product_id, valid_from)` (deterministic, stateless) |
# MAGIC
# MAGIC `valid_to` is computed via window (LEAD), not from
# MAGIC `product_pricing.effective_to`, so we don't depend on the source
# MAGIC keeping `effective_to` correctly closed. This makes the dim
# MAGIC self-consistent regardless of source-side bookkeeping.
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC `MERGE ON surrogate_key WHEN MATCHED UPDATE SET *` handles both:
# MAGIC - existing version with same `valid_from` → SCD-1 attrs refresh, plus
# MAGIC   `valid_to`/`is_current` flip when a new pricing row supersedes it
# MAGIC - new pricing row → INSERT new dim version (new surrogate key)
# MAGIC
# MAGIC Re-runs on unchanged Silver state produce zero net changes.

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

silver_products_tbl = f"{silver_catalog}.{schema_name}.products"
silver_pricing_tbl = f"{silver_catalog}.{schema_name}.product_pricing"
dim_product_tbl = f"{gold_catalog}.{schema_name}.dim_product"

print(f"pipeline_run_id      = {pipeline_run_id}")
print(f"silver_products_tbl  = {silver_products_tbl}")
print(f"silver_pricing_tbl   = {silver_pricing_tbl}")
print(f"dim_product_tbl      = {dim_product_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Silver inputs (DQ-passed only)

# COMMAND ----------

df_products = (
    spark.table(silver_products_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "product_id",
            "sku",
            "product_name",
            "category_id",
            "division",
            "brand",
            "is_active",
        )
)
products_count = df_products.count()
print(f"silver.products clean rows:        {products_count:,}")

df_pricing = (
    spark.table(silver_pricing_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "pricing_id",
            "product_id",
            "list_price",
            "cost_price",
            "effective_from",
            "pricing_type",
        )
)
pricing_count = df_pricing.count()
print(f"silver.product_pricing clean rows: {pricing_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Compute SCD-2 versions from pricing timeline
# MAGIC
# MAGIC `valid_from = effective_from`. `valid_to` derived via LEAD window —
# MAGIC the day before the next pricing row's `effective_from`. NULL for
# MAGIC the latest pricing row per product (currently active).

# COMMAND ----------

w_product_timeline = Window.partitionBy("product_id").orderBy("effective_from")

df_versioned = (
    df_pricing
        .withColumn("valid_from", F.col("effective_from"))
        .withColumn(
            "_next_effective_from",
            F.lead("effective_from").over(w_product_timeline),
        )
        .withColumn(
            "valid_to",
            F.when(
                F.col("_next_effective_from").isNotNull(),
                F.date_sub(F.col("_next_effective_from"), 1),
            ).otherwise(F.lit(None).cast("date")),
        )
        .withColumn("is_current", F.col("valid_to").isNull())
        .drop("_next_effective_from", "effective_from")
)

versions_count = df_versioned.count()
current_versions = df_versioned.filter(F.col("is_current")).count()
print(f"Computed {versions_count:,} dim versions ({current_versions:,} currently active)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Join with current product attrs (SCD-1)

# COMMAND ----------

df_dim_batch = (
    df_versioned.alias("pp")
        .join(df_products.alias("p"), on="product_id", how="inner")
        .withColumn(
            "surrogate_key",
            F.xxhash64(F.col("product_id"), F.col("valid_from")),
        )
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            "surrogate_key",
            "product_id",
            F.col("p.sku").alias("sku"),
            F.col("p.product_name").alias("product_name"),
            F.col("p.division").alias("division"),
            F.col("p.brand").alias("brand"),
            F.col("p.category_id").alias("category_id"),
            F.col("p.is_active").alias("is_active"),
            "list_price",
            "cost_price",
            "pricing_type",
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
    orphaned_pricing = versions_count - batch_count
    print(f"  WARN: {orphaned_pricing:,} pricing rows have no matching silver.products row (FK mismatch)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Ensure table + MERGE on surrogate_key

# COMMAND ----------

dim_exists = spark.catalog.tableExists(dim_product_tbl)
print(f"{dim_product_tbl} exists: {dim_exists}")

if not dim_exists:
    print(f"Creating {dim_product_tbl} for the first time")
    (
        df_dim_batch.limit(0).write
            .format("delta")
            .partitionBy("is_current")
            .saveAsTable(dim_product_tbl)
    )

staging_view = "dim_product_staging"
df_dim_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {dim_product_tbl} AS tgt
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

dim_total = spark.table(dim_product_tbl).count()
dim_current_total = spark.table(dim_product_tbl).filter(F.col("is_current") == True).count()
distinct_nks = spark.table(dim_product_tbl).select("product_id").distinct().count()

print(f"{dim_product_tbl} total rows:    {dim_total:,}")
print(f"  is_current = true:              {dim_current_total:,}")
print(f"  distinct product_id values:     {distinct_nks:,}")
print(f"  expected (current rows == NKs): {dim_current_total == distinct_nks}")

# Sanity: surrogate_key should be unique
surrogate_unique = spark.table(dim_product_tbl).select("surrogate_key").distinct().count()
print(f"  distinct surrogate_keys:        {surrogate_unique:,}  ({'OK' if surrogate_unique == dim_total else 'COLLISION'})")

display(
    spark.table(dim_product_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)
