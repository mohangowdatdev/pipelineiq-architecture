# PipelineIQ — ADF Pipeline and Medallion ETL

*Written S20 (2026-06-09), after the Tier-6 chunk-2 end-to-end smoke went green
(ADF run `215da040`, 100/100 activities Succeeded). Covers the built pipeline as
it actually runs, not the original slide-ware.*

---

## Summary

The production landing extract is the **`pl_master_copy`** Azure Data Factory
pipeline (`pipelineiq-adf-dev`), a single metadata-driven pipeline that copies all
source entities from Azure SQL (`velora_oms`) to ADLS `landing/`, writes the
PostgreSQL control plane, then triggers the Databricks medallion (bronze → silver
→ gold). It is a faithful reproduction of the laptop scaffold
`scripts/export_velora_to_landing.py`, but driven by `pipeline.entity_registry`
(via a Function endpoint) instead of a hardcoded entity list.

**Status:** built and smoke-green end-to-end. The daily trigger `trg_daily_0040`
(00:40 UTC) is still **Stopped** — until cutover, `export_velora_to_landing.py`
remains the prod path. See "Cutover & open concerns" below.

---

## Topology of one run

```
pl_master_copy(run_date = yesterday UTC)
├─ GetEntities            AzureFunctionActivity GET /api/entities
│                         → active entity_registry rows (priority, partition_date_column, …)
├─ LogRunStart            POST /runs/start  { run_id = pipeline().RunId }
├─ ForEachEntity          (batchCount 4, over GetEntities.output.entities)
│   ├─ LogEntityStart     POST /runs/start  { run_id = <RunId>:<source_table> }
│   ├─ ClearTarget        Delete landing/<table>/[date=<run_date>|full]/  (dep: Completed)
│   ├─ CopyToLanding      AzureSqlSource → ParquetSink (dep: ClearTarget Completed)
│   ├─ RegisterFile       POST /files/register   { file_path, row_count, … }
│   ├─ CommitWatermark    POST /watermarks/<entity>/commit  { last_successful_load, next_window_start }
│   ├─ LogEntityEnd       POST /runs/<RunId>:<table>/end  { status: success, rows_* }
│   └─ LogEntityFailed    POST …/end { status: failed }   (dep: CopyToLanding Failed)
├─ StartMedallion         WebActivity POST {dbx}/api/2.1/jobs/run-now (MSI)
│                         { job_id: <medallion_job_id>, notebook_params:{ pipeline_run_id } }
├─ PollMedallion          Until( contains(life_cycle, TERMINATED|INTERNAL_ERROR|SKIPPED) )
│   ├─ WaitPoll           20 s
│   ├─ GetMedallionRun    WebActivity GET {dbx}/api/2.1/jobs/runs/get?run_id=… (MSI)
│   ├─ LatchLifeCycle     SetVariable medallion_life_cycle = state.life_cycle_state
│   └─ LatchStateJson     SetVariable medallion_state_json = string(state)
├─ AssertMedallion        If contains(medallion_state_json, '"result_state":"SUCCESS"')
│                         else → Fail "MedallionFailed"
├─ LogRunEnd              POST /runs/<RunId>/end { status: success }   (dep: AssertMedallion Succeeded)
├─ LogRunFailedCopy       POST …/end { status: failed }   (dep: ForEachEntity Failed)
└─ LogRunFailedMedallion  POST …/end { status: failed }   (dep: AssertMedallion Failed)
```

Every `AzureFunctionActivity` carries `policy: { retry: 4, retryIntervalInSeconds: 30 }`
to ride out Flex-Consumption Function cold-start 503s (the nightly fire and the
post-medallion `LogRunEnd` both hit a scaled-to-zero app). DECISIONS #79–81, S20.

---

## Metadata-driven design (entity_registry)

`GetEntities` calls the Function `GET /api/entities` (`functions/function_app.py`),
which returns active `pipeline.entity_registry` rows ordered by `(priority,
entity_name)`. Each row drives one ForEach iteration. The fields that matter:

- `source_schema` / `source_table` — the Azure SQL object to copy.
- `partition_date_column` (DECISIONS #76) — **non-null only for `orders`
  (`order_date`) and `inventory_snapshot` (`snapshot_date`)**. Non-null ⇒ by-date
  extract into `landing/<table>/date=<run_date>/`; null ⇒ full dump into
  `landing/<table>/full/`. This — not `load_type` — is the by-date-vs-full switch.
- `watermark_column` / `load_type` — passed to the datasets for parity with the
  scaffold (watermark is committed per-copy; see below).

"Metadata-driven" is a **fact**, not a slide: adding a row to `entity_registry`
adds an entity to the nightly copy with no pipeline edit.

---

## Copy & landing

`CopyToLanding` reads via `ds_sql_source` (parameterised `AzureSqlTable`) and writes
`ds_adls_sink` (parameterised Parquet). The source query is built per-entity:
- full: `SELECT * FROM [schema].[table]`
- by-date: `… WHERE [partition_date_column] = '<run_date>'`

`ClearTarget` deletes the target folder first (so a re-run overwrites that
partition); its dependency is `Completed` (not `Succeeded`) so a first-run
missing-folder fault is tolerated. Landing layout matches the scaffold:
`landing/<table>/date=<YYYY-MM-DD>/` or `landing/<table>/full/`.

---

## Control plane (PostgreSQL `pipeline.*` via Function REST)

ADF never touches Postgres directly — it calls 6 Function endpoints
(`functions/function_app.py`, function-key auth, psycopg3 pool):

| Activity | Endpoint | Writes |
|---|---|---|
| GetEntities | `GET /entities` | (reads `entity_registry`) |
| LogRunStart / LogEntityStart | `POST /runs/start` | `pipeline_exec_log` INSERT (open row) |
| RegisterFile | `POST /files/register` | `file_registry` INSERT |
| CommitWatermark | `POST /watermarks/<entity>/commit` | `watermarks` UPDATE |
| LogRunEnd / *Failed | `POST /runs/<run_id>/end` | `pipeline_exec_log` UPDATE (close row) |

The commit-watermark route is **dotted** (`watermarks/velora_oms.orders/commit`) —
the entity name contains a `.`; the V2 `@app.route` handles it. **Watermark is
committed per-copy inside the ForEach**, before the medallion (DECISIONS #75) —
looser than build_order 6.8's strict end-to-end semantics, accepted under faithful
reproduction. `log_run_end` matches the open row on `run_id AND end_time IS NULL`
(DECISIONS #73), so `pipeline_exec_log` is one INSERT + one UPDATE per run.

---

## Medallion orchestration (the RunMedallion fix)

The medallion is **not** an ADF DatabricksNotebook activity (the original design,
which failed: an ADF-spawned cluster has no `data_security_mode`, hence no Unity
Catalog access — DECISIONS #78). Instead (DECISIONS #79, Option 1b):

- `core/medallion_workflow/` (Terraform, PipelineIQ-IaC) defines a `databricks_job`
  running `/Shared/pipelineiq/orchestrate_medallion` on a **`SINGLE_USER`** cluster.
  The `databricks.workspace` provider is `azure-cli`-auth, so the job is created and
  **run-as `mohan.gowda`**, who already holds the UC grants the catch-up driver uses
  → Unity Catalog access for free.
- ADF triggers it via REST: `StartMedallion` (`jobs/run-now`, **MSI** auth to the
  Azure Databricks resource `2ff814a6-…`) → `PollMedallion` (Until-poll `runs/get`,
  latching state into pipeline variables because Until-internal activities aren't
  referenceable outside the loop) → `AssertMedallion` (string-match
  `"result_state":"SUCCESS"` — ADF throws on dotting into `result_state` while the
  run is mid-flight, since the field only appears once terminal).

`orchestrate_medallion.py` chains the medallion sequentially via
`dbutils.notebook.run`, threading `pipeline_run_id` (not `run_date`):
**bronze (10 entities) → silver (10) → gold (8 tasks, dims → facts)**.

### Bronze — canonical-type enforcement (DECISIONS #80)

`landing/` can hold Parquet from two writers with divergent types for the same
column (the laptop scaffold: `decimal(9-10,2)`, `TIMESTAMP_NTZ`, `BIGINT`; ADF:
`decimal(12,2)`, `TIMESTAMP`, `INT`). `mergeSchema` only reconciles decimals;
forcing an explicit read-schema hits Parquet-converter `ClassCast`s. So bronze
reads **each sub-partition with its native schema** (vectorized, homogeneous within
one writer's folder) and **Catalyst-casts** to a canonical schema = the newest
(ADF) file's schema with **INT widened to BIGINT** (never down-casts historical
int64), then `unionByName`. `session.timeZone=UTC` so `NTZ → TIMESTAMP` preserves
wall-clock. Audit columns (`_source_file`, `_ingestion_timestamp`,
`_pipeline_run_id`, `_ingestion_date`) appended; append-only Delta partitioned by
`_ingestion_date`.

### Silver — dedup + DQ + quarantine

MERGE on business key, DQ validation, bad rows → `quarantine/` with
`rejection_reason` + `pipeline_run_id`. Three channels unified under
`channel_type`. (Notebooks per entity; unchanged in S20 beyond consuming canonical
bronze types — which propagated cleanly.)

### Gold — star schema, SCD-2, dependency order (DECISIONS #81)

9 dims + 3 facts. `dim_customer`/`dim_product` are SCD Type 2 (the S13 lazy-eval
collision fix holds: 0 collisions). **Ordering:** `static_dims` (which builds
`dim_store`) must run **before** `dim_territory` (which reads `gold.dim_store`) —
latent until the S20 clean-slate rebuild exposed it. Fixed in both the orchestrator
list and `catchup_medallion.py`'s `GOLD_TASKS` DAG.

---

## Verification (post-rebuild, S20)

The medallion was dropped and rebuilt clean-slate to adopt ADF-canonical types:

- bronze 12/12, silver 10/10, gold 12/12 tables.
- `fact_order_line` == `silver.order_lines` = **49,550** (exact).
- `fact_inventory_daily` == `silver.inventory_snapshot` = **7,380,270** (exact).
- `dim_customer` 1,060 rows == 1,060 distinct `surrogate_key` (**0 SCD-2 collisions**).
- 0 FK orphans; `bronze.orders` `total_amount`=`decimal(12,2)`,
  `created_at`/`updated_at`=`timestamp` (values byte-identical to source).

---

## Re-processing & recovery

- **Re-run a date:** fire `pl_master_copy` with `run_date=<D>` (manual:
  `az datafactory pipeline create-run … --parameters '{"run_date":"<D>"}'`).
  `ClearTarget` overwrites that landing partition; silver/gold MERGE-dedup so the
  end state is idempotent. (Bronze, being append-only, gains an ingestion snapshot.)
- **Medallion-only recovery:** `run-now` the `pipelineiq-medallion-dev` job, or
  `.venv/bin/python scripts/catchup_medallion.py --layer {bronze|silver|gold}`.
- **Inventory-only recovery:** `scripts/run_inventory_smoke.py --date <D> --force`.

---

## Cutover & open concerns

**Cutover (build_order 6.11), not yet done:** start `trg_daily_0040`
(`az datafactory trigger start … -n trg_daily_0040`, 00:40 UTC daily) and demote
`scripts/export_velora_to_landing.py` to recovery-only.

⚠️ **Decide before going autonomous:** bronze re-reads **all** of
`landing/<entity>/` and **appends** every run. This is correct for an append-only
audit layer and silver/gold stay exact via MERGE — but a **nightly** trigger grows
bronze by a full landing snapshot every night (unbounded). Options before cutover:
make bronze read only the new partition, prune old landing periodically, or accept
the growth for the project's lifespan. Tracked in CLAUDE.md `## Pending / carry-overs`.

---

## Observability

ADF factory diagnostic settings stream `PipelineRuns`/`ActivityRuns`/`TriggerRuns`
to `pipelineiq-logs-dev` (Log Analytics). Query failures via KQL there (required for
Phase 3 failure detection). The medallion job + Function App also log to the same
workspace.
