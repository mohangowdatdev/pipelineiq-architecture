# Databricks notebook source
# MAGIC %md
# MAGIC # Gold layer — static dims (no SCD-2)
# MAGIC
# MAGIC Builds the 5 static dimension tables in one notebook:
# MAGIC
# MAGIC | Table | Source | Type |
# MAGIC |---|---|---|
# MAGIC | `gold.dim_date` | Python-generated `2020-01-01` → `2030-12-31` | SCD-0 |
# MAGIC | `gold.dim_sales_channel` | 3 hardcoded rows | SCD-0 |
# MAGIC | `gold.dim_order_status` | 6 hardcoded rows | SCD-0 |
# MAGIC | `gold.dim_product_category` | `bronze.default.product_categories` | SCD-0 |
# MAGIC | `gold.dim_store` | `bronze.default.stores` | SCD-1 |
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `pipeline_run_id` | uuid | yes — falls back to a random uuid for manual runs |
# MAGIC | `bronze_catalog` | `bronze` | no |
# MAGIC | `gold_catalog` | `gold` | no |
# MAGIC
# MAGIC ## Conventions
# MAGIC
# MAGIC - All 5 tables carry `_pipeline_run_id` + `_gold_timestamp`.
# MAGIC - No `valid_from`/`valid_to`/`is_current` (static dims, per SCHEMA.md).
# MAGIC - Each table is idempotently MERGEd on its natural key — re-runs are
# MAGIC   safe.
# MAGIC - `dim_product_category` and `dim_store` deviate from SCHEMA.md's "loaded
# MAGIC   directly from source seed" wording: we route them through bronze in S10
# MAGIC   so the Gold layer never reads Azure SQL directly. Functionally
# MAGIC   identical — bronze.default.{product_categories,stores} mirror the
# MAGIC   source 1:1.

# COMMAND ----------

import uuid
from datetime import date, timedelta
from pyspark.sql import Row, functions as F
from pyspark.sql.types import (
    StructType, StructField,
    StringType, IntegerType, DateType, BooleanType,
)

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("bronze_catalog", "bronze")
dbutils.widgets.text("gold_catalog", "gold")
dbutils.widgets.text("schema_name", "default")

pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
bronze_catalog = dbutils.widgets.get("bronze_catalog").strip()
gold_catalog = dbutils.widgets.get("gold_catalog").strip()
schema_name = dbutils.widgets.get("schema_name").strip()

bronze_categories_tbl = f"{bronze_catalog}.{schema_name}.product_categories"
bronze_stores_tbl = f"{bronze_catalog}.{schema_name}.stores"

dim_date_tbl = f"{gold_catalog}.{schema_name}.dim_date"
dim_channel_tbl = f"{gold_catalog}.{schema_name}.dim_sales_channel"
dim_status_tbl = f"{gold_catalog}.{schema_name}.dim_order_status"
dim_category_tbl = f"{gold_catalog}.{schema_name}.dim_product_category"
dim_store_tbl = f"{gold_catalog}.{schema_name}.dim_store"

print(f"pipeline_run_id        = {pipeline_run_id}")
print(f"bronze_categories_tbl  = {bronze_categories_tbl}")
print(f"bronze_stores_tbl      = {bronze_stores_tbl}")
print(f"dim_date_tbl           = {dim_date_tbl}")
print(f"dim_channel_tbl        = {dim_channel_tbl}")
print(f"dim_status_tbl         = {dim_status_tbl}")
print(f"dim_category_tbl       = {dim_category_tbl}")
print(f"dim_store_tbl          = {dim_store_tbl}")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{gold_catalog}`.`{schema_name}`")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Helper: idempotent dim writer
# MAGIC
# MAGIC Each dim builds an in-memory DataFrame, then MERGEs on the natural key.
# MAGIC On first run the table is created from the empty schema; subsequent
# MAGIC runs UPSERT.

# COMMAND ----------

def write_dim(df, tbl: str, nk_col: str) -> None:
    df = (
        df
            .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
            .withColumn("_gold_timestamp", F.current_timestamp())
    )
    if not spark.catalog.tableExists(tbl):
        print(f"  creating {tbl} (first run)")
        df.limit(0).write.format("delta").saveAsTable(tbl)

    staging = f"_staging_{tbl.replace('.', '_')}"
    df.createOrReplaceTempView(staging)

    spark.sql(f"""
        MERGE INTO {tbl} AS tgt
        USING {staging} AS src
        ON tgt.{nk_col} = src.{nk_col}
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    n = spark.table(tbl).count()
    print(f"  {tbl}: {n:,} rows total after MERGE")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. `dim_date` — generate 2020-01-01 → 2030-12-31
# MAGIC
# MAGIC Indian fiscal year (Apr–Mar):
# MAGIC - `fiscal_year` for `2026-04-01` … `2027-03-31` = `2026`.
# MAGIC - `fiscal_quarter`: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar.
# MAGIC
# MAGIC Public-holiday list is a small Indian national subset (Republic Day,
# MAGIC Independence Day, Gandhi Jayanti, Christmas, New Year). Not an
# MAGIC exhaustive holiday calendar — fine for analytics demos, and easily
# MAGIC extended later.

# COMMAND ----------

PUBLIC_HOLIDAYS = {
    (1, 1):   "New Year's Day",
    (1, 26):  "Republic Day",
    (8, 15):  "Independence Day",
    (10, 2):  "Gandhi Jayanti",
    (12, 25): "Christmas Day",
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def build_dim_date_rows(start: date, end: date):
    cur = start
    while cur <= end:
        date_id = cur.year * 10000 + cur.month * 100 + cur.day
        weekday_idx = cur.weekday()  # Mon=0 .. Sun=6
        week_number = cur.isocalendar()[1]
        if cur.month >= 4:
            fiscal_year = cur.year
            fiscal_quarter = ((cur.month - 4) // 3) + 1
        else:
            fiscal_year = cur.year - 1
            fiscal_quarter = ((cur.month + 8) // 3) + 1
        holiday_name = PUBLIC_HOLIDAYS.get((cur.month, cur.day))

        yield Row(
            date_id=date_id,
            full_date=cur,
            day_of_week=DAY_NAMES[weekday_idx],
            day_number=cur.day,
            week_number=week_number,
            month_number=cur.month,
            month_name=MONTH_NAMES[cur.month - 1],
            quarter=((cur.month - 1) // 3) + 1,
            year=cur.year,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            is_weekend=weekday_idx >= 5,
            is_public_holiday=holiday_name is not None,
            holiday_name=holiday_name,
        )
        cur += timedelta(days=1)


dim_date_schema = StructType([
    StructField("date_id",           IntegerType(), False),
    StructField("full_date",         DateType(),    False),
    StructField("day_of_week",       StringType(),  False),
    StructField("day_number",        IntegerType(), False),
    StructField("week_number",       IntegerType(), False),
    StructField("month_number",      IntegerType(), False),
    StructField("month_name",        StringType(),  False),
    StructField("quarter",           IntegerType(), False),
    StructField("year",              IntegerType(), False),
    StructField("fiscal_year",       IntegerType(), False),
    StructField("fiscal_quarter",    IntegerType(), False),
    StructField("is_weekend",        BooleanType(), False),
    StructField("is_public_holiday", BooleanType(), False),
    StructField("holiday_name",      StringType(),  True),
])

date_rows = list(build_dim_date_rows(date(2020, 1, 1), date(2030, 12, 31)))
df_dim_date = spark.createDataFrame(date_rows, schema=dim_date_schema)
print(f"dim_date generated rows: {df_dim_date.count():,}")

write_dim(df_dim_date, dim_date_tbl, "date_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. `dim_sales_channel` — 3 hardcoded rows
# MAGIC
# MAGIC `channel_id` matches `silver.orders.channel_type` (`D2C` | `B2B` | `STORE`).

# COMMAND ----------

channel_rows = [
    Row(channel_id="D2C",   channel_name="Direct to Consumer",  channel_type="D2C",   description="Online direct-to-consumer ecommerce orders"),
    Row(channel_id="B2B",   channel_name="Business to Business", channel_type="B2B",   description="Sales-rep-driven B2B orders"),
    Row(channel_id="STORE", channel_name="Physical Store",       channel_type="STORE", description="In-store retail purchases"),
]
df_dim_channel = spark.createDataFrame(channel_rows)
write_dim(df_dim_channel, dim_channel_tbl, "channel_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. `dim_order_status` — 6 hardcoded rows
# MAGIC
# MAGIC Matches the 6 known `velora_oms.orders.status` values per SCHEMA.md.

# COMMAND ----------

status_rows = [
    Row(status_id="PENDING",    status_name="Pending",    status_category="ACTIVE",    sort_order=1),
    Row(status_id="PROCESSING", status_name="Processing", status_category="ACTIVE",    sort_order=2),
    Row(status_id="SHIPPED",    status_name="Shipped",    status_category="ACTIVE",    sort_order=3),
    Row(status_id="DELIVERED",  status_name="Delivered",  status_category="CLOSED",    sort_order=4),
    Row(status_id="CANCELLED",  status_name="Cancelled",  status_category="EXCEPTION", sort_order=5),
    Row(status_id="RETURNED",   status_name="Returned",   status_category="EXCEPTION", sort_order=6),
]
df_dim_status = spark.createDataFrame(status_rows)
write_dim(df_dim_status, dim_status_tbl, "status_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. `dim_product_category` — read from bronze
# MAGIC
# MAGIC Bronze is append-only, so we dedup on `category_id` (latest
# MAGIC `_bronze_timestamp`) before projecting to the Gold schema.

# COMMAND ----------

from pyspark.sql.window import Window

w_cat = Window.partitionBy("category_id").orderBy(F.col("_bronze_timestamp").desc())

df_dim_category = (
    spark.table(bronze_categories_tbl)
        .withColumn("_row_num", F.row_number().over(w_cat))
        .filter(F.col("_row_num") == 1)
        .select(
            "category_id",
            "category_name",
            "sub_category",
            "division",
            "is_active",
        )
)
print(f"dim_product_category source rows: {df_dim_category.count():,}")
write_dim(df_dim_category, dim_category_tbl, "category_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. `dim_store` — read from bronze
# MAGIC
# MAGIC Same dedup-on-PK pattern as `dim_product_category`. Carries
# MAGIC `territory_id` through unchanged — it's the FK referenced by
# MAGIC `gold.dim_territory` (synthesized later in chunk 2) and by
# MAGIC `gold.fact_order_line` (STORE channel) per SCHEMA.md.

# COMMAND ----------

w_store = Window.partitionBy("store_id").orderBy(F.col("_bronze_timestamp").desc())

df_dim_store = (
    spark.table(bronze_stores_tbl)
        .withColumn("_row_num", F.row_number().over(w_store))
        .filter(F.col("_row_num") == 1)
        .select(
            "store_id",
            "store_name",
            "city",
            "state",
            "territory_id",
            "store_tier",
            "is_active",
            "opened_date",
        )
)
print(f"dim_store source rows: {df_dim_store.count():,}")
write_dim(df_dim_store, dim_store_tbl, "store_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Verify

# COMMAND ----------

for tbl in [dim_date_tbl, dim_channel_tbl, dim_status_tbl, dim_category_tbl, dim_store_tbl]:
    n = spark.table(tbl).count()
    print(f"{tbl:50s} {n:,} rows")

display(spark.table(dim_store_tbl).limit(5))
