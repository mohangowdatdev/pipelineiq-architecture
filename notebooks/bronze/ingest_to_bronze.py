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
from functools import reduce
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DateType,
    IntegerType,
    LongType,
    NullType,
    NumericType,
    StructType,
    StructField,
    TimestampType,
)

# COMMAND ----------

dbutils.widgets.text("entity_name", "orders")
dbutils.widgets.text("pipeline_run_id", "")
dbutils.widgets.text("run_date", "")
dbutils.widgets.text("landing_account", "pipelineiqadlsdev")
dbutils.widgets.text("bronze_catalog", "bronze")
dbutils.widgets.text("bronze_schema", "default")

entity_name = dbutils.widgets.get("entity_name").strip()
pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
# run_date scopes ingestion (DECISIONS #82, incremental bronze):
#   "<YYYY-MM-DD>" -> incremental: ingest only that day's partition, idempotently.
#   "" / "ALL"     -> rebuild: ingest every landing partition in one pass (one-time
#                     backfill / cleanup; derives _load_date per-file from the path).
run_date = dbutils.widgets.get("run_date").strip()
rebuild_mode = run_date == "" or run_date.upper() == "ALL"
landing_account = dbutils.widgets.get("landing_account").strip()
bronze_catalog = dbutils.widgets.get("bronze_catalog").strip()
bronze_schema = dbutils.widgets.get("bronze_schema").strip()

if not entity_name:
    raise ValueError("entity_name is required")

print(f"entity_name      = {entity_name}")
print(f"pipeline_run_id  = {pipeline_run_id}")
print(f"run_date         = {run_date or '(ALL — rebuild)'}")
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

# Canonical-type enforcement (S20, DECISIONS #79). landing can hold Parquet from
# two writers with divergent schemas for the SAME column: the laptop export
# scaffold (pyarrow inference -> decimal(9..10,2), TIMESTAMP_NTZ, int64/BIGINT)
# and ADF (from the SQL column type -> decimal(12,2), TIMESTAMP, int32/INT). ADF's
# types are canonical (they derive from the real source schema).
#
# A single recursive read can't reconcile these: `mergeSchema` only widens
# decimals (it refuses INT vs BIGINT, TIMESTAMP vs TIMESTAMP_NTZ), and forcing an
# explicit read-schema hits low-level Parquet-converter ClassCasts (MutableInt vs
# MutableLong). So instead we cast in the engine, not the reader:
#   1. canonical = the newest landing file's schema (the most recent ADF write;
#      post-cutover the only writer), with INT widened to BIGINT so historical
#      int64 export columns never down-cast.
#   2. read each landing sub-partition (date=*/ or full/) with its NATIVE schema
#      — homogeneous within one writer's folder, so the fast vectorized reader
#      works with no conversion.
#   3. Catalyst-cast each to the canonical schema (int->long, decimal->decimal,
#      ntz->timestamp all unify cleanly as logical casts) and unionByName.
# session timeZone = UTC so the scaffold's naive TIMESTAMP_NTZ values cast to the
# same wall-clock UTC instant ADF already writes.
spark.conf.set("spark.sql.session.timeZone", "UTC")


def _canonical_parquet(root: str) -> str:
    """Pick the file whose schema defines the canonical bronze types.

    ADF Copy Activity is the canonical writer (DECISIONS #80) and names its
    output `data_<guid>_<guid>.parquet`; the laptop recovery scaffold names its
    files `<entity>_<hash>.parquet` with NARROWER inferred types (decimal(10,2),
    timestamp[us], large_string). The canonical schema MUST come from an ADF
    file so every scaffold file widens INTO it — never the reverse.

    Originally this picked "the newest file by modification time" as a proxy for
    "the most recent ADF write". That proxy breaks the moment the scaffold runs
    AFTER an ADF copy (e.g. a recovery backfill of a gap), making a narrower
    scaffold file the newest and forcing lossy narrowing casts on the ADF data.
    So: prefer the newest ADF (`data_*`) write; fall back to the newest file
    overall only when no ADF file exists (a date that was only ever scaffold-
    landed pre-cutover).
    """
    stack = [root]
    newest_adf = None  # (path, mtime) among data_*.parquet (ADF writes)
    newest_any = None  # (path, mtime) among all .parquet (fallback)
    while stack:
        for f in dbutils.fs.ls(stack.pop()):
            if f.name.endswith("/"):
                stack.append(f.path)
            elif f.path.endswith(".parquet"):
                if newest_any is None or f.modificationTime > newest_any[1]:
                    newest_any = (f.path, f.modificationTime)
                if f.name.startswith("data_") and (
                    newest_adf is None or f.modificationTime > newest_adf[1]
                ):
                    newest_adf = (f.path, f.modificationTime)
    chosen = newest_adf or newest_any
    if chosen is None:
        raise RuntimeError(f"No Parquet under {root} — has the landing extract run for {entity_name}?")
    return chosen[0]


def _widen(dt):
    return LongType() if isinstance(dt, IntegerType) else dt


_adf_schema = spark.read.parquet(_canonical_parquet(landing_path)).schema
canonical_schema = StructType([StructField(f.name, _widen(f.dataType), f.nullable) for f in _adf_schema])
print(f"canonical schema (ADF write, INT widened):\n{canonical_schema.simpleString()}")

# landing folders are NOT writer-homogeneous: a full-load entity's `full/`
# folder accumulates BOTH ADF files (timestamps as Parquet INT96) and scaffold-
# recovery files (timestamp_ntz). A single spark.read spanning them crashes the
# vectorized Parquet converter ("Unable to create Parquet converter for
# timestamp_ntz whose Parquet type is INT96") because one inferred schema can't
# cover both. A single FILE is always one writer, so read each file on its own
# native schema, then Catalyst-cast to canonical and union.
def _all_parquet_files(root: str) -> list:
    stack, files = [root], []
    while stack:
        for f in dbutils.fs.ls(stack.pop()):
            if f.name.endswith("/"):
                stack.append(f.path)
            elif f.path.endswith(".parquet"):
                files.append(f.path)
    return files


# Align each file to the canonical schema column-by-column. The scaffold infers
# Parquet types from pandas, so an all-NULL column lands with the wrong type:
# e.g. `territory_assignments.assigned_to` is a DATE that's NULL for every
# current row, which pandas types as INT — and Spark refuses INT->DATE. Treat
# any such mis-inferred / missing / NullType column as a typed NULL rather than
# an illegal cast; everything else is a normal widening cast to canonical.
def _aligned_col(src_types: dict, field) -> "F.Column":
    src = src_types.get(field.name)
    mis_inferred = isinstance(src, NumericType) and isinstance(
        field.dataType, (DateType, TimestampType)
    )
    if src is None or isinstance(src, NullType) or mis_inferred:
        return F.lit(None).cast(field.dataType).alias(field.name)
    return F.col(field.name).cast(field.dataType).alias(field.name)


def _read_aligned(path: str):
    df = spark.read.parquet(path)
    src_types = {f.name: f.dataType for f in df.schema}
    return df.select([_aligned_col(src_types, f) for f in canonical_schema])


# Detect load type from the landing layout and scope the read (DECISIONS #82):
#   by-date entity  (date=*/ present) -> incremental: read only date=<run_date>/;
#                                        rebuild:     read every date=*/ partition.
#   full-load entity (full/ present)  -> always read full/ (one snapshot; ADF
#                                        ClearTarget-overwrites it each copy).
_subdirs = [f.name.rstrip("/") for f in dbutils.fs.ls(landing_path) if f.name.endswith("/")]
is_by_date = any(s.startswith("date=") for s in _subdirs)
is_full_load = (not is_by_date) and ("full" in _subdirs)

def _newest_file(root: str) -> str:
    """Newest .parquet under root by modification time (the latest snapshot)."""
    newest = None
    stack = [root]
    while stack:
        for f in dbutils.fs.ls(stack.pop()):
            if f.name.endswith("/"):
                stack.append(f.path)
            elif f.path.endswith(".parquet") and (newest is None or f.modificationTime > newest[1]):
                newest = (f.path, f.modificationTime)
    return newest[0] if newest else None


if is_full_load:
    # `full/` is a complete snapshot; read only the LATEST file. ADF ClearTarget-
    # overwrites it to a single file, but a scaffold backfill can leave several
    # full snapshots behind — reading all would multiply rows. Newest = freshest
    # complete picture. (Full-load tables here are small, single-file per write.)
    _newest = _newest_file(landing_path + "full/")
    _files = [_newest] if _newest else []
elif is_by_date and not rebuild_mode:
    _files = _all_parquet_files(landing_path + f"date={run_date}/")
elif is_by_date:  # rebuild: every day's partition
    _files = _all_parquet_files(landing_path)
else:  # unrecognized layout — read everything (back-compat)
    _files = _all_parquet_files(landing_path)

print(f"load type        = {'full' if is_full_load else 'by-date' if is_by_date else 'flat'}")
print(f"read scope       = {len(_files)} file(s)")

if not _files and is_by_date and not rebuild_mode:
    # An incremental night with no landing for this date (e.g. ADF copied 0 rows):
    # nothing to ingest — exit cleanly rather than fail the medallion.
    dbutils.notebook.exit(f"bronze {entity_name}: no landing for date={run_date}, skipped")

_parts = [_read_aligned(p) for p in _files]
df_landing = reduce(lambda a, b: a.unionByName(b), _parts)

source_count = df_landing.count()
print(f"Read {source_count:,} rows from {len(_files)} file(s) under {landing_path}")

if source_count == 0:
    raise RuntimeError(
        f"No rows read from {read_roots} for {entity_name} — has ADF landed this "
        f"partition? (run_date={run_date or 'ALL'})"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Append the audit columns
# MAGIC
# MAGIC `_source_file` is set per-row from the actual file each row came from
# MAGIC (Spark's `input_file_name()`). `_load_date` is the business date the row
# MAGIC belongs to and is the Bronze partition key (DECISIONS #82): for by-date
# MAGIC entities it's `run_date` (or, in rebuild, parsed from the `date=` folder);
# MAGIC for full-load snapshots it's the run/refresh date. The rest are per-run
# MAGIC constants.

# COMMAND ----------

if is_by_date and rebuild_mode:
    # rebuild: each row's load date = the date= partition the file came from.
    _load_date_col = F.to_date(
        F.regexp_extract(F.input_file_name(), r"date=(\d{4}-\d{2}-\d{2})", 1)
    )
elif is_by_date:
    _load_date_col = F.lit(run_date).cast(DateType())
else:  # full-load / flat snapshot
    _load_date_col = F.lit(run_date).cast(DateType()) if not rebuild_mode else F.current_date()

df_bronze = (
    df_landing
        .withColumn("_source_file", F.input_file_name())
        .withColumn("_ingestion_timestamp", F.current_timestamp())
        .withColumn("_pipeline_run_id", F.lit(pipeline_run_id))
        .withColumn("_bronze_timestamp", F.current_timestamp())
        .withColumn("_ingestion_date", F.to_date(F.col("_ingestion_timestamp")))
        .withColumn("_load_date", _load_date_col)
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
target_plain = target.replace("`", "")

# Incremental + idempotent write (DECISIONS #82). Bronze is partitioned by
# `_load_date` and only ever touches the partition(s) this run ingested, so a
# nightly trigger grows Bronze by one day — not a full re-append of all landing.
#   full-load : replace the whole table with the latest `full/` snapshot.
#   rebuild   : drop + recreate fresh (all days), used by the one-time backfill.
#   increment : replaceWhere just this run_date's partition — re-running a night
#               overwrites that day rather than duplicating it.
writer = df_bronze.write.format("delta").partitionBy("_load_date")

if is_full_load:
    # rebuild drops first to re-partition cleanly (old tables were partitioned by
    # _ingestion_date); steady-state nightly just overwrites the snapshot.
    if rebuild_mode:
        spark.sql(f"DROP TABLE IF EXISTS {target_plain}")
    writer.mode("overwrite").option("overwriteSchema", "true").saveAsTable(target_plain)
elif rebuild_mode:
    spark.sql(f"DROP TABLE IF EXISTS {target_plain}")
    writer.mode("overwrite").option("overwriteSchema", "true").saveAsTable(target_plain)
else:
    (
        writer.mode("overwrite")
        .option("replaceWhere", f"_load_date = '{run_date}'")
        .option("mergeSchema", "true")
        .saveAsTable(target_plain)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Verify

# COMMAND ----------

bronze_count = spark.table(target_plain).count()
written = df_bronze.count()
print(f"{target} now holds {bronze_count:,} rows total ({written:,} written this run).")

display(
    spark.table(target_plain)
        .groupBy("_load_date")
        .count()
        .orderBy(F.col("_load_date").desc())
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
