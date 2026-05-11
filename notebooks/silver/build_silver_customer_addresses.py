# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.customer_addresses → silver.customer_addresses + quarantine.customer_addresses
# MAGIC
# MAGIC Trailing-edge silver — no current Gold consumer. Built so the medallion
# MAGIC contract holds end-to-end across all source entities; future address-
# MAGIC analytics work (e.g. shipping-territory dim, geographic facts) reads
# MAGIC from silver, not bronze.
# MAGIC
# MAGIC Reads `bronze.default.customer_addresses` (mutable at source — addresses
# MAGIC get added; `is_primary` flips), dedups by `address_id` keeping the
# MAGIC latest `_bronze_timestamp`, runs DQ rules, MERGEs into
# MAGIC `silver.default.customer_addresses`.
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
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `address_id` IS NULL | `NULL_ADDRESS_ID` |
# MAGIC | `customer_id` IS NULL | `NULL_CUSTOMER_ID` |
# MAGIC | `customer_id` not in `bronze.customers` | `UNKNOWN_CUSTOMER_ID` |
# MAGIC | `address_type` not in known set | `INVALID_ADDRESS_TYPE` |
# MAGIC | `pincode` IS NULL | `NULL_PINCODE` |
# MAGIC | `pincode` not 6-digit Indian PIN | `INVALID_PINCODE` |
# MAGIC
# MAGIC Known address types: `HOME`, `WORK`, `BILLING`, `SHIPPING`.
# MAGIC PIN validation: 6 digits, no leading 0 (Indian PIN convention).
# MAGIC No SCD tracking — Gold doesn't carry historical address versions.

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

bronze_addr_tbl = f"{bronze_catalog}.{schema_name}.customer_addresses"
bronze_customers_tbl = f"{bronze_catalog}.{schema_name}.customers"
silver_addr_tbl = f"{silver_catalog}.{schema_name}.customer_addresses"
quarantine_addr_tbl = f"{quarantine_catalog}.{schema_name}.customer_addresses"

print(f"pipeline_run_id     = {pipeline_run_id}")
print(f"bronze_addr_tbl     = {bronze_addr_tbl}")
print(f"silver_addr_tbl     = {silver_addr_tbl}")
print(f"quarantine_addr_tbl = {quarantine_addr_tbl}")

VALID_ADDRESS_TYPES = ["HOME", "WORK", "BILLING", "SHIPPING"]

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze + dedup on `address_id`

# COMMAND ----------

df_bronze = spark.table(bronze_addr_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_addr_tbl}")

dedup_window = Window.partitionBy("address_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)

dedup_count = df_dedup.count()
print(f"After dedup on address_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. FK reference set — known customer_ids in bronze.customers

# COMMAND ----------

df_known_customers = (
    spark.table(bronze_customers_tbl)
        .select("customer_id")
        .distinct()
)
known_customer_count = df_known_customers.count()
print(f"Reference set: {known_customer_count:,} distinct customer_ids in {bronze_customers_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Tag each row with DQ flags

# COMMAND ----------

df_with_fk = (
    df_dedup.alias("a")
        .join(
            df_known_customers.alias("c").withColumn("_customer_known", F.lit(True)),
            on="customer_id",
            how="left",
        )
        .withColumn("_customer_known", F.coalesce(F.col("_customer_known"), F.lit(False)))
)

valid_addr_types_arr = F.array(*[F.lit(t) for t in VALID_ADDRESS_TYPES])

df_dq = (
    df_with_fk
        .withColumn("_v_null_address_id",   F.when(F.col("address_id").isNull(),                                    F.lit("NULL_ADDRESS_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_customer_id",  F.when(F.col("customer_id").isNull(),                                   F.lit("NULL_CUSTOMER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_customer",  F.when(F.col("customer_id").isNotNull() & (~F.col("_customer_known")),  F.lit("UNKNOWN_CUSTOMER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_addr_type", F.when(~F.array_contains(valid_addr_types_arr, F.col("address_type")),  F.lit("INVALID_ADDRESS_TYPE")).otherwise(F.lit(None)))
        .withColumn("_v_null_pincode",      F.when(F.col("pincode").isNull(),                                       F.lit("NULL_PINCODE")).otherwise(F.lit(None)))
        # Indian PIN: exactly 6 digits, first digit 1-9
        .withColumn(
            "_v_invalid_pincode",
            F.when(
                F.col("pincode").isNotNull() & ~F.col("pincode").rlike(r"^[1-9][0-9]{5}$"),
                F.lit("INVALID_PINCODE"),
            ).otherwise(F.lit(None)),
        )
)

violation_cols = [
    "_v_null_address_id",
    "_v_null_customer_id",
    "_v_unknown_customer",
    "_v_invalid_addr_type",
    "_v_null_pincode",
    "_v_invalid_pincode",
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
        .drop("_violations_array", "_customer_known", *violation_cols)
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
        .withColumn("is_primary", F.col("is_primary").cast("boolean"))
        .select(
            F.col("address_id"),
            F.col("customer_id"),
            F.col("address_line"),
            F.col("city"),
            F.col("state"),
            F.col("pincode"),
            F.col("is_primary"),
            F.col("address_type"),
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

silver_table_exists = spark.catalog.tableExists(silver_addr_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_addr_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_addr_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into `silver.customer_addresses`

# COMMAND ----------

staging_view = "silver_customer_addresses_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_addr_tbl} AS tgt
USING {staging_view} AS src
ON tgt.address_id = src.address_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.customer_addresses`

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("address_id"), F.col("customer_id"),
                F.col("address_line"), F.col("city"), F.col("state"),
                F.col("pincode"), F.col("is_primary"), F.col("address_type"),
                F.col("_source_file"),
            )))
            .withColumn("source_table", F.lit("velora_crm.customer_addresses"))
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
            .saveAsTable(quarantine_addr_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_addr_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_addr_tbl).count()
print(f"{silver_addr_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_addr_tbl).count()
    if spark.catalog.tableExists(quarantine_addr_tbl) else 0
)
print(f"{quarantine_addr_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_addr_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)
