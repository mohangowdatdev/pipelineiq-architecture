# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — silver.customers → gold.dim_customer (SCD Type 2)
# MAGIC
# MAGIC Reads `silver.default.customers` (current state per `customer_id`) and
# MAGIC applies SCD Type 2 logic against `gold.default.dim_customer`. Tracked
# MAGIC attributes are `segment` and `city` (DECISIONS #15). Changes to other
# MAGIC fields (e.g. `email`) are SCD Type 1 — overwrite in place.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `pipeline_run_id` | uuid | yes — falls back to a random uuid for manual runs |
# MAGIC | `silver_catalog` | `silver` | no |
# MAGIC | `gold_catalog` | `gold` | no |
# MAGIC
# MAGIC ## SCD Type 2 algorithm
# MAGIC
# MAGIC For each `customer_id` in silver:
# MAGIC
# MAGIC | Case | Trigger | Action |
# MAGIC |---|---|---|
# MAGIC | **New** | no current dim row exists | INSERT new dim row, `valid_from = today`, `valid_to = NULL`, `is_current = true` |
# MAGIC | **Changed (tracked attr)** | dim row exists AND `segment` or `city` differs | UPDATE existing: `valid_to = today - 1 day`, `is_current = false`; INSERT new version |
# MAGIC | **Changed (other attr only)** | dim row exists AND only e.g. `email` differs | UPDATE existing: overwrite the SCD-1 fields in place |
# MAGIC | **Unchanged** | dim row exists AND nothing differs | no-op |
# MAGIC
# MAGIC `surrogate_key` is `xxhash64(customer_id, valid_from)` — deterministic,
# MAGIC stateless, no sequence required. Re-runs of the same (`customer_id`,
# MAGIC `valid_from`) pair produce the same key.

# COMMAND ----------

import uuid
from pyspark.sql import functions as F

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("silver_catalog", "silver")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
silver_catalog = dbutils.widgets.get("silver_catalog").strip()
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

silver_customers_tbl = f"{silver_catalog}.{schema_name}.customers"
silver_orders_tbl = f"{silver_catalog}.{schema_name}.orders"
dim_customer_tbl = f"{gold_catalog}.{schema_name}.dim_customer"

print(f"pipeline_run_id      = {pipeline_run_id}")
print(f"silver_customers_tbl = {silver_customers_tbl}")
print(f"silver_orders_tbl    = {silver_orders_tbl}")
print(f"dim_customer_tbl     = {dim_customer_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read Silver customers (clean, deduped current state)

# COMMAND ----------

df_silver = (
    spark.table(silver_customers_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "customer_id",
            "full_name",
            "email",
            "segment",
            "city",
            "state",
            "account_type",
            "is_active",
        )
)
silver_count = df_silver.count()
print(f"Read {silver_count:,} clean rows from {silver_customers_tbl}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Read current dim state (or empty if first run)

# COMMAND ----------

dim_exists = spark.catalog.tableExists(dim_customer_tbl)
print(f"{dim_customer_tbl} exists: {dim_exists}")

if dim_exists:
    df_dim_current = (
        spark.table(dim_customer_tbl)
            .filter(F.col("is_current") == True)
            .select(
                F.col("customer_id").alias("_dim_customer_id"),
                F.col("surrogate_key").alias("_dim_surrogate_key"),
                F.col("segment").alias("_dim_segment"),
                F.col("city").alias("_dim_city"),
                F.col("full_name").alias("_dim_full_name"),
                F.col("email").alias("_dim_email"),
                F.col("state").alias("_dim_state"),
                F.col("account_type").alias("_dim_account_type"),
                F.col("is_active").alias("_dim_is_active"),
            )
    )
else:
    df_dim_current = spark.createDataFrame(
        [],
        "_dim_customer_id STRING, _dim_surrogate_key BIGINT, "
        "_dim_segment STRING, _dim_city STRING, _dim_full_name STRING, "
        "_dim_email STRING, _dim_state STRING, _dim_account_type STRING, "
        "_dim_is_active BOOLEAN",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Categorise each silver row vs. dim
# MAGIC
# MAGIC `_action` is one of `NEW`, `SCD2_CHANGE`, `SCD1_OVERWRITE`, `UNCHANGED`.

# COMMAND ----------

df_joined = (
    df_silver.alias("s")
        .join(df_dim_current.alias("d"),
              F.col("s.customer_id") == F.col("d._dim_customer_id"),
              "left")
)

scd2_changed = (F.col("segment") != F.col("_dim_segment")) | (F.col("city") != F.col("_dim_city"))
scd1_changed = (
    (F.col("full_name")    != F.col("_dim_full_name")) |
    (F.col("email")        != F.col("_dim_email")) |
    (F.col("state")        != F.col("_dim_state")) |
    (F.col("account_type") != F.col("_dim_account_type")) |
    (F.col("is_active")    != F.col("_dim_is_active"))
)

df_actioned = (
    df_joined
        .withColumn(
            "_action",
            F.when(F.col("_dim_customer_id").isNull(), F.lit("NEW"))
             .when(scd2_changed,                       F.lit("SCD2_CHANGE"))
             .when(scd1_changed,                       F.lit("SCD1_OVERWRITE"))
             .otherwise(                               F.lit("UNCHANGED")),
        )
)

action_counts = df_actioned.groupBy("_action").count().collect()
for row in action_counts:
    print(f"  {row['_action']}: {row['count']:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Close out SCD2-changed rows in dim
# MAGIC
# MAGIC `UPDATE gold.dim_customer SET valid_to = current_date() - 1,
# MAGIC is_current = false WHERE surrogate_key IN (changed surrogate keys)`.

# COMMAND ----------

if dim_exists:
    df_to_close = (
        df_actioned.filter(F.col("_action") == "SCD2_CHANGE")
            .select(F.col("_dim_surrogate_key").alias("surrogate_key"))
    )
    close_count = df_to_close.count()

    if close_count > 0:
        df_to_close.createOrReplaceTempView("dim_customer_to_close")
        spark.sql(f"""
            MERGE INTO {dim_customer_tbl} AS tgt
            USING dim_customer_to_close AS src
            ON tgt.surrogate_key = src.surrogate_key
            WHEN MATCHED THEN UPDATE SET
                valid_to = date_sub(current_date(), 1),
                is_current = false
        """)
        print(f"Closed {close_count:,} SCD2 rows.")
    else:
        print("No SCD2 changes — nothing to close.")
else:
    print("First run — no existing rows to close.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Apply SCD1 overwrites in place

# COMMAND ----------

if dim_exists:
    df_scd1 = (
        df_actioned.filter(F.col("_action") == "SCD1_OVERWRITE")
            .select(
                F.col("_dim_surrogate_key").alias("surrogate_key"),
                "full_name", "email", "state", "account_type", "is_active",
            )
    )
    scd1_count = df_scd1.count()

    if scd1_count > 0:
        df_scd1.createOrReplaceTempView("dim_customer_scd1")
        spark.sql(f"""
            MERGE INTO {dim_customer_tbl} AS tgt
            USING dim_customer_scd1 AS src
            ON tgt.surrogate_key = src.surrogate_key
            WHEN MATCHED THEN UPDATE SET
                tgt.full_name    = src.full_name,
                tgt.email        = src.email,
                tgt.state        = src.state,
                tgt.account_type = src.account_type,
                tgt.is_active    = src.is_active
        """)
        print(f"Overwrote {scd1_count:,} SCD1 rows in place.")
    else:
        print("No SCD1 overwrites needed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Build new dim rows for NEW + SCD2_CHANGE
# MAGIC
# MAGIC **`valid_from` rule (per SCHEMA.md):**
# MAGIC - For `NEW` rows: earliest known activity date for that customer =
# MAGIC   `MIN(silver.orders.order_date)`. If no orders yet, fall back to
# MAGIC   `current_date()`. This makes as-of joins from `fact_order_line` work
# MAGIC   correctly on historical orders placed before the dim was first
# MAGIC   written.
# MAGIC - For `SCD2_CHANGE` rows: `current_date()` — the change was detected
# MAGIC   today, so the new version's first valid day is today. (The closed
# MAGIC   prior version's `valid_to` is set to `current_date() - 1` in step 4.)
# MAGIC
# MAGIC `channel_type` mirrors `account_type` (a D2C customer's channel is
# MAGIC D2C; a B2B customer's channel is B2B). Customers can place STORE
# MAGIC orders too, but that's a fact-level concern — the *customer* doesn't
# MAGIC have a STORE channel attribute.
# MAGIC
# MAGIC `surrogate_key = xxhash64(customer_id, valid_from)` is deterministic
# MAGIC and stateless. Re-runs produce identical keys.

# COMMAND ----------

# Earliest order date per customer — used as `valid_from` for NEW rows.
df_first_activity = (
    spark.table(silver_orders_tbl)
        .filter(F.col("dq_passed") == True)
        .groupBy("customer_id")
        .agg(F.min("order_date").alias("_first_activity_date"))
)

df_to_insert = (
    df_actioned.filter(F.col("_action").isin("NEW", "SCD2_CHANGE")).alias("a")
        .join(df_first_activity.alias("fa"),
              on="customer_id", how="left")
        .withColumn(
            "valid_from",
            F.when(
                F.col("_action") == "NEW",
                F.coalesce(F.col("_first_activity_date"), F.current_date()),
            ).otherwise(F.current_date()),
        )
        .withColumn("valid_to", F.lit(None).cast("date"))
        .withColumn("is_current", F.lit(True))
        .withColumn(
            "surrogate_key",
            F.xxhash64(F.col("customer_id"), F.col("valid_from")),
        )
        .withColumn("channel_type", F.col("account_type"))
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            "surrogate_key", "customer_id", "full_name", "email", "segment",
            "city", "state", "account_type", "channel_type", "is_active",
            "valid_from", "valid_to", "is_current",
            "_pipeline_run_id", "_gold_timestamp",
        )
)
insert_count = df_to_insert.count()
print(f"New dim rows to insert: {insert_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Ensure schema + create table on first run, then append

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

if not dim_exists:
    print(f"Creating {dim_customer_tbl} for the first time")
    (
        df_to_insert.write
            .format("delta")
            .partitionBy("is_current")
            .saveAsTable(dim_customer_tbl)
    )
elif insert_count > 0:
    (
        df_to_insert.write
            .format("delta")
            .mode("append")
            .saveAsTable(dim_customer_tbl)
    )
else:
    print("No new dim rows to insert.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Verify

# COMMAND ----------

dim_total = spark.table(dim_customer_tbl).count()
dim_current_total = spark.table(dim_customer_tbl).filter(F.col("is_current") == True).count()
distinct_nks = spark.table(dim_customer_tbl).select("customer_id").distinct().count()

print(f"{dim_customer_tbl} total rows:   {dim_total:,}")
print(f"  is_current = true:               {dim_current_total:,}")
print(f"  distinct customer_id values:     {distinct_nks:,}")
print(f"  expected (current rows == NKs):  {dim_current_total == distinct_nks}")

display(
    spark.table(dim_customer_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern notes
# MAGIC
# MAGIC - SCD Type 2 close-old / insert-new uses two separate operations:
# MAGIC   `UPDATE` (close) followed by `APPEND` (new version). MERGE alone
# MAGIC   can't express "match on natural key but insert a new row" cleanly.
# MAGIC - SCD Type 1 attributes (`email`, `full_name`, `state`, etc.) are
# MAGIC   overwritten in place via a separate MERGE — they don't trigger an
# MAGIC   SCD-2 version split.
# MAGIC - Idempotency: re-running on the same Silver state produces no
# MAGIC   `NEW` or `SCD2_CHANGE` actions (everything's `UNCHANGED`).
# MAGIC - Surrogate key is `xxhash64(customer_id, valid_from)` — deterministic
# MAGIC   per (NK, valid_from) pair. If two SCD-2 changes happened on the same
# MAGIC   day, the second would collide with the first; for this project's
# MAGIC   daily-batch cadence that's not a real risk. If we ever process
# MAGIC   intra-day batches, switch to `xxhash64(customer_id, valid_from, _gold_timestamp)`.
