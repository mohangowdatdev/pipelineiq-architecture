# Databricks notebook source
# MAGIC %md
# MAGIC # Medallion orchestrator — bronze → silver → gold
# MAGIC
# MAGIC Single entry point invoked **once** by the ADF master pipeline
# MAGIC (`pl_master_copy`) over the `ls_databricks` (MSI) linked service, after the
# MAGIC ForEach copy stage has landed every entity in `landing/`.
# MAGIC
# MAGIC It chains the proven medallion notebooks via `dbutils.notebook.run` on the
# MAGIC current cluster, reusing the exact entity lists + gold dependency DAG from
# MAGIC `scripts/catchup_medallion.py` (the offline catch-up driver). DECISIONS #77.
# MAGIC
# MAGIC ## Why one notebook instead of ~30 chained ADF activities
# MAGIC
# MAGIC build_order 6.5 originally sketched separate Bronze/Silver/Gold ADF notebook
# MAGIC activities. Encoding the gold dim→fact DAG as ADF activities in Bicep is
# MAGIC fragile; running it from one orchestrator notebook keeps the DAG in Python
# MAGIC (where it already lives + is tested) and is the clean first exercise of the
# MAGIC ADF MSI → Databricks linked service.
# MAGIC
# MAGIC ## Widget inputs
# MAGIC
# MAGIC | name | example | required |
# MAGIC |---|---|---|
# MAGIC | `pipeline_run_id` | ADF run id | no — ADF passes `@{pipeline().RunId}`; falls back to a random uuid for manual runs |
# MAGIC | `run_date` | `2026-06-09` | no — ADF passes `@{pipeline().parameters.run_date}`; empty = "ALL" (rebuild) |
# MAGIC
# MAGIC ## Contract notes
# MAGIC
# MAGIC - Bronze is **incremental** (DECISIONS #82): it ingests only `run_date`'s
# MAGIC   landing partition (by-date entities) or the latest `full/` snapshot
# MAGIC   (full-load entities), idempotently. `run_date` is threaded to Bronze only;
# MAGIC   Silver/Gold take just `pipeline_run_id`, read all of Bronze/Silver and
# MAGIC   MERGE-dedup, so they stay date-agnostic.
# MAGIC - Empty `run_date` ("ALL") puts Bronze in rebuild mode (ingest every landing
# MAGIC   partition in one pass) — used for the one-time backfill, not nightly runs.
# MAGIC - Bronze covers the 10 dynamic entities (catch-up parity). `product_categories`
# MAGIC   + `stores` are landed by ADF (registry-driven) but their bronze tables stay
# MAGIC   the S9.5 hydration; gold `static_dims` reads those existing bronze tables.
# MAGIC - Execution is sequential. Gold tasks are listed in topological order
# MAGIC   (dims → facts), so sequential calls honor the dependency DAG with no extra
# MAGIC   wiring. (Bronze + silver could fan out via ThreadPoolExecutor to cut wall
# MAGIC   time; sequential is the correct, simplest baseline.)

# COMMAND ----------

import uuid

# COMMAND ----------

dbutils.widgets.text("pipeline_run_id", "")
pipeline_run_id = dbutils.widgets.get("pipeline_run_id").strip() or str(uuid.uuid4())
print(f"pipeline_run_id = {pipeline_run_id}")

# run_date scopes Bronze ingestion to one day's landing partition (DECISIONS #82).
# ADF passes the pipeline's run_date; a manual/empty value means "ALL" (rebuild —
# ingest every landing partition). Only Bronze consumes it; Silver/Gold read all
# of Bronze/Silver and MERGE-dedup, so they stay date-agnostic.
dbutils.widgets.text("run_date", "")
run_date = dbutils.widgets.get("run_date").strip()
print(f"run_date        = {run_date or '(ALL — rebuild)'}")

# Per-notebook timeout (seconds). Generous — inventory silver is the long pole.
NOTEBOOK_TIMEOUT_S = 3600

# Workspace notebook roots (mirror scripts/catchup_medallion.py upload targets).
BRONZE_NOTEBOOK_WS = "/Shared/pipelineiq/bronze/ingest_to_bronze"
SILVER_DIR_WS = "/Shared/pipelineiq/silver"
GOLD_DIR_WS = "/Shared/pipelineiq/gold"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Entity lists + gold DAG
# MAGIC
# MAGIC Kept in lock-step with `scripts/catchup_medallion.py:38-80`. If that file
# MAGIC changes, change here too.

# COMMAND ----------

# Bronze: one entity-agnostic notebook, run once per entity (10 dynamic entities).
BRONZE_ENTITIES = [
    "orders",
    "order_lines",
    "order_status_log",
    "customers",
    "customer_addresses",
    "products",
    "product_pricing",
    "inventory_snapshot",
    "sales_reps",
    "territory_assignments",
]

# Silver: one notebook per entity (build_silver_<e>).
SILVER_ENTITIES = [
    "orders",
    "order_lines",
    "order_status_log",
    "customers",
    "customer_addresses",
    "products",
    "product_pricing",
    "inventory_snapshot",
    "sales_reps",
    "territory_assignments",
]

# Gold: one notebook per dim/fact (build_gold_<entity>), in topological order.
# `static_dims` builds 5 reference dims (incl. dim_store). Facts follow the dims
# they read.
#
# Order note (S20): `static_dims` MUST precede `dim_territory` — build_gold_dim_
# territory reads gold.dim_store (built by static_dims) to collect observed
# territory_ids. catchup_medallion.py declares dim_territory with depends_on=[]
# (a latent bug), which only worked because catch-up runs always had a pre-
# existing dim_store; a clean from-scratch rebuild (gold dropped) exposes it.
# Sequential execution here honours this list order, so static_dims is placed
# first. (catchup_medallion.py's GOLD_TASKS DAG carries the matching fix.)
GOLD_ENTITIES = [
    "dim_customer",
    "dim_product",
    "dim_sales_rep",
    "static_dims",
    "dim_territory",
    "fact_order_line",
    "fact_inventory_daily",
    "fact_daily_channel_revenue",
]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Runner

# COMMAND ----------


def run_notebook(path: str, params: dict) -> None:
    print(f"  → {path}  {params}")
    result = dbutils.notebook.run(path, NOTEBOOK_TIMEOUT_S, params)
    # dbutils.notebook.run raises on a notebook exception, so reaching here is a
    # success. Surface the child's dbutils.notebook.exit() value if it set one.
    if result:
        print(f"    exit: {result}")


def run_layer(name: str, calls: list[tuple[str, dict]]) -> None:
    print(f"\n=== {name} ({len(calls)} notebooks) ===")
    for path, params in calls:
        run_notebook(path, params)
    print(f"=== {name} complete ===")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute: bronze → silver → gold

# COMMAND ----------

run_layer(
    "bronze",
    [
        (
            BRONZE_NOTEBOOK_WS,
            {"entity_name": e, "pipeline_run_id": pipeline_run_id, "run_date": run_date},
        )
        for e in BRONZE_ENTITIES
    ],
)

# COMMAND ----------

run_layer(
    "silver",
    [
        (f"{SILVER_DIR_WS}/build_silver_{e}", {"pipeline_run_id": pipeline_run_id})
        for e in SILVER_ENTITIES
    ],
)

# COMMAND ----------

run_layer(
    "gold",
    [
        (f"{GOLD_DIR_WS}/build_gold_{e}", {"pipeline_run_id": pipeline_run_id})
        for e in GOLD_ENTITIES
    ],
)

# COMMAND ----------

dbutils.notebook.exit(
    f"medallion complete: pipeline_run_id={pipeline_run_id}, "
    f"bronze={len(BRONZE_ENTITIES)} silver={len(SILVER_ENTITIES)} gold={len(GOLD_ENTITIES)}"
)
