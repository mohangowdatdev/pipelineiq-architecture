# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — silver.order_lines + silver.orders → gold.fact_order_line
# MAGIC
# MAGIC Grain: one row per SKU per order. PK = pass-through `line_id`
# MAGIC (DECISIONS #58 — already a UUID, globally unique, no synthesis).
# MAGIC
# MAGIC ## FK lookups
# MAGIC
# MAGIC | Dim | Join semantics |
# MAGIC |---|---|
# MAGIC | `dim_date` | `cast(order_date as YYYYMMDD INT)` |
# MAGIC | `dim_customer` | as-of join on `order_date` (DECISIONS #56) |
# MAGIC | `dim_product` | as-of join on `order_date` |
# MAGIC | `dim_sales_channel` | direct on `channel_type` (= `channel_id`) |
# MAGIC | `dim_sales_rep` | as-of join on `order_date` (only for B2B; LEFT for D2C/STORE) |
# MAGIC | `dim_store` | direct on `store_id` (only for STORE; LEFT for D2C/B2B) |
# MAGIC | `dim_order_status` | direct on `silver.orders.status` (= `status_id`) |
# MAGIC
# MAGIC ## As-of join robustness (floor at earliest dim version)
# MAGIC
# MAGIC Standard as-of: pick the latest dim version where `valid_from <=
# MAGIC order_date <= COALESCE(valid_to, '9999-12-31')`. **Floor:** if no
# MAGIC version covers `order_date` (i.e. `order_date < MIN(valid_from)` for
# MAGIC that NK), fall back to the earliest version. This handles cases where
# MAGIC the source SCD timeline started later than some fact dates (e.g. a
# MAGIC product has price history starting 2026-04-01 but was ordered on
# MAGIC 2026-03-15) — the fact still gets a sensible dim attribution rather
# MAGIC than dropping out.
# MAGIC
# MAGIC ## territory_id derivation (DECISIONS #55)
# MAGIC
# MAGIC | Channel | territory_id |
# MAGIC |---|---|
# MAGIC | STORE | `dim_store.territory_id` (lookup by `store_id`) |
# MAGIC | B2B | `dim_sales_rep.territory_id` (carried through from as-of join) |
# MAGIC | D2C | `'D2C_NATIONAL'` sentinel |
# MAGIC
# MAGIC ## Measures (DECISIONS #54)
# MAGIC
# MAGIC | Measure | Formula |
# MAGIC |---|---|
# MAGIC | `quantity_ordered` | pass-through `silver.order_lines.quantity` |
# MAGIC | `unit_price_at_sale` | pass-through `silver.order_lines.unit_price` |
# MAGIC | `discount_amount` | pass-through `silver.order_lines.discount_amt` |
# MAGIC | `line_total_inr` | pass-through `silver.order_lines.line_total` (post-discount, source-computed) |
# MAGIC | `tax_amount` | `round(line_total_inr * 0.18, 2)` (India GST) |
# MAGIC | `net_revenue_inr` | `line_total_inr` (= post-discount, pre-tax) |
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC MERGE on `line_id`. Re-runs UPSERT in place — measure values are
# MAGIC source-stable, dim surrogate keys are stable (xxhash64 is
# MAGIC deterministic per (NK, valid_from)).

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

silver_lines_tbl = f"{silver_catalog}.{schema_name}.order_lines"
silver_orders_tbl = f"{silver_catalog}.{schema_name}.orders"
dim_customer_tbl = f"{gold_catalog}.{schema_name}.dim_customer"
dim_product_tbl = f"{gold_catalog}.{schema_name}.dim_product"
dim_sales_rep_tbl = f"{gold_catalog}.{schema_name}.dim_sales_rep"
dim_store_tbl = f"{gold_catalog}.{schema_name}.dim_store"
fact_tbl = f"{gold_catalog}.{schema_name}.fact_order_line"

print(f"pipeline_run_id   = {pipeline_run_id}")
print(f"silver_lines_tbl  = {silver_lines_tbl}")
print(f"silver_orders_tbl = {silver_orders_tbl}")
print(f"fact_tbl          = {fact_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read silver inputs (DQ-passed only) + enrich lines with order context

# COMMAND ----------

df_lines = (
    spark.table(silver_lines_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "line_id",
            "order_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount_amt",
            "line_total",
            F.col("_ingestion_timestamp").alias("_silver_ingestion_timestamp"),
        )
)
lines_count = df_lines.count()
print(f"silver.order_lines clean rows: {lines_count:,}")

df_orders = (
    spark.table(silver_orders_tbl)
        .filter(F.col("dq_passed") == True)
        .select(
            "order_id",
            "customer_id",
            "channel_type",
            "order_date",
            "status",
            "store_id",
            "rep_id",
        )
)
orders_count = df_orders.count()
print(f"silver.orders clean rows:      {orders_count:,}")

df_lo = df_lines.join(df_orders, on="order_id", how="inner")
lo_count = df_lo.count()
print(f"after lines ⋈ orders:          {lo_count:,}  ({lines_count - lo_count:,} lines orphaned from orders)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. As-of join helper
# MAGIC
# MAGIC Returns a DataFrame with `<prefix>_surrogate_key` (and any
# MAGIC additional carried columns) attached. Implements primary as-of
# MAGIC + earliest-version floor in one pass via row_number ranking.

# COMMAND ----------

def asof_attach(
    df_facts,
    fact_join_col: str,        # column on df_facts (the NK)
    fact_date_col: str,        # column on df_facts (the event date)
    dim_table: str,            # gold.<dim>
    dim_join_col: str,         # NK column on dim
    surrogate_alias: str,      # name to give the dim's surrogate_key on fact
    extra_cols: list[tuple[str, str]] = None,  # [(dim_col, fact_alias), ...]
    fact_pk_col: str = "line_id",
):
    """As-of attach with floor-at-earliest fallback.

    Ranking key:
      1. CASE WHEN dim.valid_from <= fact_date THEN 0 ELSE 1 END  (in-range first)
      2. WITHIN in-range: dim.valid_from DESC                      (latest preceding)
      3. WITHIN out-of-range: dim.valid_from ASC                   (earliest after)
    """
    df_dim = (
        spark.table(dim_table)
            .select(
                F.col(dim_join_col).alias("_dim_nk"),
                F.col("surrogate_key").alias(f"_dim_{surrogate_alias}"),
                F.col("valid_from").alias("_dim_valid_from"),
                F.col("valid_to").alias("_dim_valid_to"),
                *[F.col(c).alias(f"_dim_extra_{i}") for i, (c, _) in enumerate(extra_cols or [])],
            )
    )

    df_pairs = (
        df_facts.alias("f")
            .join(df_dim.alias("d"),
                  F.col(f"f.{fact_join_col}") == F.col("d._dim_nk"),
                  "left")
    )

    is_in_range = (
        (F.col("_dim_valid_from") <= F.col(fact_date_col)) &
        (F.coalesce(F.col("_dim_valid_to"), F.lit("9999-12-31").cast("date")) >= F.col(fact_date_col))
    )

    w_rank = (
        Window.partitionBy(fact_pk_col)
            .orderBy(
                F.when(is_in_range, F.lit(0)).otherwise(F.lit(1)).asc(),
                F.when(is_in_range, F.col("_dim_valid_from")).otherwise(F.lit(None)).desc_nulls_last(),
                F.when(~is_in_range, F.col("_dim_valid_from")).otherwise(F.lit(None)).asc_nulls_last(),
            )
    )

    df_ranked = (
        df_pairs
            .withColumn("_rn", F.row_number().over(w_rank))
            .filter(F.col("_rn") == 1)
    )

    df_out = df_ranked.withColumnRenamed(f"_dim_{surrogate_alias}", surrogate_alias)
    for i, (_, fact_alias) in enumerate(extra_cols or []):
        df_out = df_out.withColumnRenamed(f"_dim_extra_{i}", fact_alias)

    return df_out.drop("_dim_nk", "_dim_valid_from", "_dim_valid_to", "_rn")


# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Attach dim_customer surrogate

# COMMAND ----------

df_with_customer = asof_attach(
    df_lo,
    fact_join_col="customer_id",
    fact_date_col="order_date",
    dim_table=dim_customer_tbl,
    dim_join_col="customer_id",
    surrogate_alias="customer_surrogate_key",
)
unmatched_customer = df_with_customer.filter(F.col("customer_surrogate_key").isNull()).count()
print(f"dim_customer attach: {df_with_customer.count():,} rows, {unmatched_customer:,} unmatched")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Attach dim_product surrogate

# COMMAND ----------

df_with_product = asof_attach(
    df_with_customer,
    fact_join_col="product_id",
    fact_date_col="order_date",
    dim_table=dim_product_tbl,
    dim_join_col="product_id",
    surrogate_alias="product_surrogate_key",
)
unmatched_product = df_with_product.filter(F.col("product_surrogate_key").isNull()).count()
print(f"dim_product attach:  {df_with_product.count():,} rows, {unmatched_product:,} unmatched")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Attach dim_sales_rep surrogate (only for B2B; pulls rep's territory_id through)
# MAGIC
# MAGIC For non-B2B rows (`rep_id` IS NULL), surrogate stays NULL. We do
# MAGIC the as-of attach only on rows with non-null `rep_id` to avoid
# MAGIC inflating the partition count.

# COMMAND ----------

df_b2b = df_with_product.filter(F.col("rep_id").isNotNull())
df_non_b2b = (
    df_with_product.filter(F.col("rep_id").isNull())
        .withColumn("rep_surrogate_key", F.lit(None).cast("bigint"))
        .withColumn("rep_territory_id", F.lit(None).cast("string"))
)

if df_b2b.count() > 0:
    df_b2b_with_rep = asof_attach(
        df_b2b,
        fact_join_col="rep_id",
        fact_date_col="order_date",
        dim_table=dim_sales_rep_tbl,
        dim_join_col="rep_id",
        surrogate_alias="rep_surrogate_key",
        extra_cols=[("territory_id", "rep_territory_id")],
    )
else:
    df_b2b_with_rep = df_b2b.withColumn("rep_surrogate_key", F.lit(None).cast("bigint")) \
                            .withColumn("rep_territory_id", F.lit(None).cast("string"))

df_with_rep = df_b2b_with_rep.unionByName(df_non_b2b, allowMissingColumns=False)
unmatched_b2b_rep = df_b2b_with_rep.filter(F.col("rep_surrogate_key").isNull()).count()
print(f"dim_sales_rep attach (B2B only): {df_b2b_with_rep.count():,} B2B rows, {unmatched_b2b_rep:,} unmatched")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Attach store's territory_id (only for STORE channel)

# COMMAND ----------

df_dim_store = spark.table(dim_store_tbl).select(
    F.col("store_id").alias("_store_id"),
    F.col("territory_id").alias("store_territory_id"),
)

df_with_store_territory = (
    df_with_rep.alias("f")
        .join(df_dim_store.alias("ds"),
              F.col("f.store_id") == F.col("ds._store_id"),
              "left")
        .drop("_store_id")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Final projection — territory_id per channel, measures, audit

# COMMAND ----------

df_fact = (
    df_with_store_territory
        .withColumn(
            "territory_id",
            F.when(F.col("channel_type") == "STORE", F.col("store_territory_id"))
             .when(F.col("channel_type") == "B2B",   F.col("rep_territory_id"))
             .when(F.col("channel_type") == "D2C",   F.lit("D2C_NATIONAL"))
             .otherwise(F.lit(None)),
        )
        .withColumn(
            "order_date_id",
            (F.year("order_date") * 10000 +
             F.month("order_date") * 100 +
             F.dayofmonth("order_date")).cast("int"),
        )
        .withColumn("channel_id", F.col("channel_type"))
        .withColumn("status_id", F.col("status"))
        # tax_amount uses F.expr so 0.18 is parsed as a decimal literal
        # (not a Python float → IEEE Double, which introduces ~0.5% rounding
        # drift vs the SCHEMA.md spec `round(line_total_inr * 0.18, 2)`).
        .withColumn("tax_amount", F.expr("round(line_total * 0.18, 2)"))
        .withColumn("net_revenue_inr", F.col("line_total"))
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_gold_timestamp", F.current_timestamp())
        .select(
            F.col("line_id"),
            F.col("order_id"),
            F.col("order_date_id"),
            F.col("customer_surrogate_key"),
            F.col("product_surrogate_key"),
            F.col("channel_id"),
            F.col("rep_surrogate_key"),
            F.col("store_id"),
            F.col("territory_id"),
            F.col("status_id"),
            F.col("quantity").alias("quantity_ordered"),
            F.col("unit_price").alias("unit_price_at_sale"),
            F.col("discount_amt").alias("discount_amount"),
            F.col("line_total").alias("line_total_inr"),
            F.col("tax_amount"),
            F.col("net_revenue_inr"),
            F.col("_pipeline_run_id"),
            F.col("_silver_ingestion_timestamp").alias("_ingestion_timestamp"),
            F.col("_gold_timestamp"),
        )
)

batch_count = df_fact.count()
print(f"Fact batch rows ready to MERGE: {batch_count:,}")

# Sanity counts
unmatched_final = df_fact.filter(
    F.col("customer_surrogate_key").isNull() | F.col("product_surrogate_key").isNull()
).count()
unmatched_territory = df_fact.filter(F.col("territory_id").isNull()).count()
print(f"  unmatched customer or product surrogate: {unmatched_final:,}")
print(f"  unmatched territory_id:                  {unmatched_territory:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Ensure table + MERGE on line_id

# COMMAND ----------

fact_exists = spark.catalog.tableExists(fact_tbl)
print(f"{fact_tbl} exists: {fact_exists}")

if not fact_exists:
    print(f"Creating {fact_tbl} for the first time")
    (
        df_fact.limit(0).write
            .format("delta")
            .partitionBy("order_date_id")
            .saveAsTable(fact_tbl)
    )

staging_view = "fact_order_line_staging"
df_fact.createOrReplaceTempView(staging_view)

merge_sql = f"""
MERGE INTO {fact_tbl} AS tgt
USING {staging_view} AS src
ON tgt.line_id = src.line_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""
print("Running MERGE...")
spark.sql(merge_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Verify

# COMMAND ----------

fact_total = spark.table(fact_tbl).count()
distinct_lines = spark.table(fact_tbl).select("line_id").distinct().count()

print(f"{fact_tbl} total rows:          {fact_total:,}")
print(f"  distinct line_id values:       {distinct_lines:,} ({'OK' if fact_total == distinct_lines else 'PK COLLISION'})")

# Channel breakdown
print("\nChannel breakdown:")
display(spark.table(fact_tbl).groupBy("channel_id").count().orderBy("channel_id"))

# Revenue summary
print("\nRevenue summary:")
display(
    spark.table(fact_tbl).agg(
        F.sum("quantity_ordered").alias("total_units"),
        F.round(F.sum("line_total_inr"), 2).alias("total_line_total"),
        F.round(F.sum("tax_amount"), 2).alias("total_tax"),
        F.round(F.sum("net_revenue_inr"), 2).alias("total_net_revenue"),
    )
)

display(
    spark.table(fact_tbl)
        .orderBy(F.col("_gold_timestamp").desc())
        .limit(5)
)
