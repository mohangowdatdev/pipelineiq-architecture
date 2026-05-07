# Databricks notebook source
# MAGIC %md
# MAGIC # Silver layer — bronze.orders → silver.orders + quarantine.orders
# MAGIC
# MAGIC Reads `bronze.default.orders` (append-only history), dedups by `order_id`
# MAGIC keeping the latest `_bronze_timestamp`, runs DQ rules, then MERGEs the
# MAGIC clean batch into `silver.default.orders`. Rejected rows are appended to
# MAGIC `quarantine.default.orders` with a rejection reason and the original
# MAGIC record JSON-serialised in `raw_record`.
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
# MAGIC - Silver is **deduplicated** on the business key (`order_id`).
# MAGIC - Silver is **DQ-validated** — each row carries `dq_passed` + `dq_rejection_reason`.
# MAGIC - Bad rows are **quarantined**, never silently dropped.
# MAGIC - MERGE on business key — re-runs are idempotent.
# MAGIC - Three channels (`D2C`, `B2B`, `STORE`) unified under `channel_type`.
# MAGIC
# MAGIC ## DQ rules applied
# MAGIC
# MAGIC | Rule | Rejection code |
# MAGIC |---|---|
# MAGIC | `order_id` IS NULL | `NULL_ORDER_ID` |
# MAGIC | `customer_id` IS NULL | `NULL_CUSTOMER_ID` |
# MAGIC | `customer_id` not in `bronze.customers` | `UNKNOWN_CUSTOMER_ID` |
# MAGIC | `channel_type` not in (`D2C`,`B2B`,`STORE`) | `INVALID_CHANNEL_TYPE` |
# MAGIC | `order_date` IS NULL | `NULL_ORDER_DATE` |
# MAGIC | `status` IS NULL | `NULL_STATUS` |
# MAGIC | `total_amount` IS NULL OR `<= 0` | `INVALID_TOTAL_AMOUNT` |
# MAGIC | STORE channel without `store_id` | `STORE_MISSING_STORE_ID` |
# MAGIC | B2B channel without `rep_id` | `B2B_MISSING_REP_ID` |
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

bronze_orders_tbl = f"{bronze_catalog}.{schema_name}.orders"
bronze_customers_tbl = f"{bronze_catalog}.{schema_name}.customers"
silver_orders_tbl = f"{silver_catalog}.{schema_name}.orders"
quarantine_orders_tbl = f"{quarantine_catalog}.{schema_name}.orders"

print(f"pipeline_run_id      = {pipeline_run_id}")
print(f"bronze_orders_tbl    = {bronze_orders_tbl}")
print(f"bronze_customers_tbl = {bronze_customers_tbl}")
print(f"silver_orders_tbl    = {silver_orders_tbl}")
print(f"quarantine_orders_tbl= {quarantine_orders_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Bronze orders + dedup on `order_id`
# MAGIC
# MAGIC Bronze is append-only — every re-run of the Bronze notebook adds a fresh
# MAGIC copy of every source row. Silver's job is to collapse that history to one
# MAGIC row per business key, taking the most recent (`_bronze_timestamp DESC`).

# COMMAND ----------

df_bronze = spark.table(bronze_orders_tbl)
bronze_count = df_bronze.count()
print(f"Read {bronze_count:,} rows from {bronze_orders_tbl}")

dedup_window = Window.partitionBy("order_id").orderBy(F.col("_bronze_timestamp").desc())

df_dedup = (
    df_bronze
        .withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
)

dedup_count = df_dedup.count()
print(f"After dedup on order_id: {dedup_count:,} rows ({bronze_count - dedup_count:,} duplicates collapsed)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build the customer-id reference set for FK validation

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
# MAGIC
# MAGIC Left-join against the customer reference set, then apply the rules. Each
# MAGIC rule contributes a string token to `_violations` (or empty if it passes).
# MAGIC The final `dq_rejection_reason` is the `;`-joined non-empty tokens, or
# MAGIC NULL when every rule passed.

# COMMAND ----------

df_with_fk = (
    df_dedup.alias("o")
        .join(
            df_known_customers.alias("c").withColumn("_customer_known", F.lit(True)),
            on="customer_id",
            how="left",
        )
        .withColumn("_customer_known", F.coalesce(F.col("_customer_known"), F.lit(False)))
)

VALID_CHANNELS = ["D2C", "B2B", "STORE"]

df_dq = (
    df_with_fk
        .withColumn("_v_null_order_id",     F.when(F.col("order_id").isNull(),                                  F.lit("NULL_ORDER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_null_customer_id",  F.when(F.col("customer_id").isNull(),                               F.lit("NULL_CUSTOMER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_unknown_customer",  F.when(F.col("customer_id").isNotNull() & (~F.col("_customer_known")), F.lit("UNKNOWN_CUSTOMER_ID")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_channel",   F.when(~F.col("channel_type").isin(VALID_CHANNELS),                 F.lit("INVALID_CHANNEL_TYPE")).otherwise(F.lit(None)))
        .withColumn("_v_null_order_date",   F.when(F.col("order_date").isNull(),                                F.lit("NULL_ORDER_DATE")).otherwise(F.lit(None)))
        .withColumn("_v_null_status",       F.when(F.col("status").isNull(),                                    F.lit("NULL_STATUS")).otherwise(F.lit(None)))
        .withColumn("_v_invalid_total",     F.when(F.col("total_amount").isNull() | (F.col("total_amount") <= 0), F.lit("INVALID_TOTAL_AMOUNT")).otherwise(F.lit(None)))
        .withColumn("_v_store_no_store_id", F.when((F.col("channel_type") == "STORE") & F.col("store_id").isNull(), F.lit("STORE_MISSING_STORE_ID")).otherwise(F.lit(None)))
        .withColumn("_v_b2b_no_rep_id",     F.when((F.col("channel_type") == "B2B")   & F.col("rep_id").isNull(),    F.lit("B2B_MISSING_REP_ID")).otherwise(F.lit(None)))
)

violation_cols = [
    "_v_null_order_id",
    "_v_null_customer_id",
    "_v_unknown_customer",
    "_v_invalid_channel",
    "_v_null_order_date",
    "_v_null_status",
    "_v_invalid_total",
    "_v_store_no_store_id",
    "_v_b2b_no_rep_id",
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
# MAGIC ## 4. Build the Silver-shaped clean batch
# MAGIC
# MAGIC Project to the Silver schema (per SCHEMA.md `silver.orders`) and stamp
# MAGIC `_silver_timestamp` + `_pipeline_run_id`.

# COMMAND ----------

df_silver_batch = (
    df_tagged.filter(F.col("dq_passed"))
        .withColumn("_silver_timestamp", F.current_timestamp())
        .select(
            F.col("order_id"),
            F.col("customer_id"),
            F.col("channel_type"),
            F.col("order_date"),
            F.col("status"),
            F.col("store_id"),
            F.col("rep_id"),
            F.col("total_amount"),
            F.col("currency"),
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
# MAGIC ## 5. Ensure target schemas exist; create silver table on first run
# MAGIC
# MAGIC On first run `silver.default.orders` does not exist — `MERGE` requires a
# MAGIC pre-existing table, so we materialise an empty shell with the right
# MAGIC schema first. Subsequent runs no-op the create.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{silver_catalog}`.`{schema_name}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{quarantine_catalog}`.`{schema_name}`")

silver_table_exists = spark.catalog.tableExists(silver_orders_tbl)

if not silver_table_exists:
    print(f"Creating empty {silver_orders_tbl} with the Silver schema")
    (
        df_silver_batch.limit(0).write
            .format("delta")
            .partitionBy("_silver_date")
            .saveAsTable(silver_orders_tbl)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE the clean batch into `silver.orders`
# MAGIC
# MAGIC `MERGE` on `order_id`. Existing rows are updated only when the incoming
# MAGIC `_silver_timestamp` is newer (defensive; dedup already filters older),
# MAGIC new rows are inserted.

# COMMAND ----------

staging_view = "silver_orders_staging"
df_silver_batch.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {silver_orders_tbl} AS tgt
USING {staging_view} AS src
ON tgt.order_id = src.order_id
WHEN MATCHED AND src._silver_timestamp >= tgt._silver_timestamp THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""

print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Append rejected rows to `quarantine.orders`
# MAGIC
# MAGIC Schema per SCHEMA.md `quarantine.orders` — `quarantine_id`,
# MAGIC `rejection_reason`, `pipeline_run_id`, `rejected_at`, `raw_record`
# MAGIC (JSON), `source_table`.

# COMMAND ----------

if failed_count > 0:
    df_quarantine = (
        df_tagged.filter(~F.col("dq_passed"))
            .withColumn("quarantine_id", F.expr("uuid()"))
            .withColumn("rejection_reason", F.col("dq_rejection_reason"))
            .withColumn("pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("rejected_at", F.current_timestamp())
            .withColumn("raw_record", F.to_json(F.struct(
                F.col("order_id"), F.col("customer_id"), F.col("channel_type"),
                F.col("order_date"), F.col("status"), F.col("store_id"),
                F.col("rep_id"), F.col("total_amount"), F.col("currency"),
                F.col("_source_file"), F.col("_pipeline_run_id"),
            )))
            .withColumn("source_table", F.lit("velora_oms.orders"))
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
            .saveAsTable(quarantine_orders_tbl)
    )
    print(f"Appended {failed_count:,} rows to {quarantine_orders_tbl}")
else:
    print("No DQ failures — nothing to quarantine.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

silver_total = spark.table(silver_orders_tbl).count()
print(f"{silver_orders_tbl} now holds {silver_total:,} rows total")

quarantine_total = (
    spark.table(quarantine_orders_tbl).count()
    if spark.catalog.tableExists(quarantine_orders_tbl) else 0
)
print(f"{quarantine_orders_tbl} now holds {quarantine_total:,} rows total")

display(
    spark.table(silver_orders_tbl)
        .orderBy(F.col("_silver_timestamp").desc())
        .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern notes
# MAGIC
# MAGIC - This notebook is **entity-specific** (unlike Bronze which is entity-
# MAGIC   agnostic per DECISIONS #48). Silver carries per-entity DQ rules, so
# MAGIC   each Silver entity gets its own notebook — `build_silver_orders.py`,
# MAGIC   `build_silver_order_lines.py`, etc.
# MAGIC - MERGE-on-business-key keeps re-runs idempotent. Replaying any window
# MAGIC   of Bronze yields the same Silver state.
# MAGIC - Quarantine is **append-only**. Same row may be re-quarantined on each
# MAGIC   run (different `quarantine_id`, same `rejection_reason`). De-dup is a
# MAGIC   reporting concern, not a write-time concern.
# MAGIC - DQ rules are evaluated independently — a row violating 3 rules ends
# MAGIC   up quarantined once with `rejection_reason = "RULE_A;RULE_B;RULE_C"`,
# MAGIC   not three separate quarantine rows.
