# Runbook: Injecting Failure Scenarios

*Written end of Phase 1. Updated as downstream detection is verified in Phases 3-5.*

---

## Purpose

This runbook walks through triggering each of the 6 PipelineIQ failure scenarios.
Use it during development to verify that each failure class produces the expected
detection event, RCA summary, and Slack alert. Use it during demos to walk stakeholders
through what each failure looks like and how PipelineIQ responds.

Each scenario is fully deterministic with the default seed (42). Running the same
failure command twice with the same seed and date produces identical bad data.

---

## Prerequisites

```bash
# Ensure Azure SQL is accessible
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD -Q "SELECT 1"

# Ensure generator environment is ready
cd generator
source ../venv/bin/activate

# Verify a clean dry-run passes before injecting
python main.py --date 2025-01-15 --dry-run
```

---

## Scenario 1: Schema Drift

**What it injects:** Adds a `promo_code VARCHAR(20)` column to the orders DataFrame
before writing to Azure SQL.

**Why this breaks things:** The ADF Copy Activity in landing will accept any column
(it writes Parquet — schema-on-read). The Bronze notebook reads Parquet and also
accepts extra columns. But the Silver notebook runs a MERGE against the silver.orders
Delta table, which was created with a fixed schema. The extra column is not in the
target schema, and the MERGE fails with a schema mismatch error.

### Commands

```bash
# Inject
python main.py --date 2025-01-15 --failure schema_drift

# Verify in Azure SQL — orders table will have extra promo_code column values
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT TOP 5 order_id, promo_code FROM velora_oms.orders WHERE promo_code IS NOT NULL"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds — Parquet accepts extra columns |
| Bronze | Succeeds — schema-on-read |
| Silver | FAILS — MERGE schema mismatch on promo_code column |
| Gold | Does not run (Silver failed) |

### Expected PipelineIQ response

- Log Analytics detects Silver notebook failure
- RCA summary: *"New column 'promo_code' found in source orders data that is not in the Silver schema. Enable schema evolution on the silver.orders Delta table or explicitly add the column to the Silver definition."*
- Affected component: `silver.orders`
- Slack alert fires within 5 minutes

### Reset to clean data

```bash
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "ALTER TABLE velora_oms.orders DROP COLUMN IF EXISTS promo_code"
```

---

## Scenario 2: Referential Integrity Violation

**What it injects:** Sets `product_id = '00000000-0000-0000-0000-000000009999'` on 30
order_lines. This product ID does not exist in `velora_pim.products`.

**Why this breaks things:** The Silver notebook performs a DQ check that joins
order_lines to products and flags any line where the product_id has no match.
These 30 lines are routed to quarantine with `rejection_reason = 'UNKNOWN_PRODUCT_ID'`.

### Commands

```bash
# Inject
python main.py --date 2025-01-15 --failure referential_integrity

# Verify — should see 30 order_lines with the ghost product_id
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT COUNT(*) FROM velora_oms.order_lines WHERE product_id = '00000000-0000-0000-0000-000000009999'"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds |
| Bronze | Succeeds — appends all rows |
| Silver | DQ check: 30 lines quarantined with UNKNOWN_PRODUCT_ID |
| Gold | Runs on valid records — 30 lines absent from fact_order_line |

### Expected PipelineIQ response

- Detection: `rows_rejected > 0` in pipeline_exec_log for silver.order_lines
- RCA summary: *"30 order_lines reference product_id 9999 which does not exist in the product master. This is typically caused by new SKUs introduced in the PIM system that have not yet synced to the pipeline."*
- Affected component: `silver.order_lines`

### Reset

No action needed — the ghost product_id rows are in Azure SQL but Silver routes them
to quarantine and they never appear in Gold. Run a clean data day to flush the pipeline.

---

## Scenario 3: Volume Anomaly

**What it injects:** Truncates the orders batch to exactly 12 orders regardless of
the day-of-week and seasonal volume that the generator would normally produce
(typically 280-580 orders on a Friday in November).

**Why this is the most important scenario:** Traditional monitoring watches for errors.
This scenario has no errors. The pipeline runs successfully. ADF reports success.
All notebooks complete with exit code 0. But 96% of expected orders are missing.
Without a volume-aware observability layer, this data loss would be completely invisible.

### Commands

```bash
# Inject
python main.py --date 2025-01-15 --failure volume_anomaly

# Verify — total orders for this date should be exactly 12
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT COUNT(*) FROM velora_oms.orders WHERE order_date = '2025-01-15'"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds |
| Bronze | Succeeds — 12 rows |
| Silver | Succeeds — 12 rows |
| Gold | Succeeds — 12 rows in fact_order_line |

### Expected PipelineIQ response

- All notebook runs report success
- PipelineIQ volume detection: today's count (12) vs 7-day rolling average (~380)
  — delta is 96.8% below average, threshold is 30% — alert fires
- RCA summary: *"Pipeline completed successfully but order volume (12) is 96.8% below the 7-day rolling average (380). This indicates silent data loss, likely a POS system outage or data feed interruption. No pipeline errors were raised."*
- Affected component: `velora_oms.orders`

### Reset

Run a normal day's generator run for the next date. The volume alert
will clear once normal counts resume.

---

## Scenario 4: Null Constraint Violation

**What it injects:** Sets `unit_price = NULL` on 50 order_lines.

**Why this breaks things:** The Silver DQ rule requires `unit_price IS NOT NULL AND unit_price > 0`.
The 50 affected lines are quarantined with `rejection_reason = 'NULL_REQUIRED_FIELD'`.

### Commands

```bash
# Inject
python main.py --date 2025-01-15 --failure null_constraint

# Verify
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT COUNT(*) FROM velora_oms.order_lines WHERE unit_price IS NULL"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds |
| Bronze | Succeeds — null unit_price is valid at Bronze (no business rules) |
| Silver | DQ check: 50 lines quarantined with NULL_REQUIRED_FIELD |
| Gold | Runs on valid records |

### Expected PipelineIQ response

- Detection: rows_rejected > 0 in pipeline_exec_log
- RCA summary: *"50 order_lines have null unit_price. This is typically caused by a pricing lookup returning no result for new SKUs that were introduced today but whose pricing records have not yet been created."*
- Affected component: `silver.order_lines`

### Reset

No action needed. The 50 null lines remain in Azure SQL and Bronze
but are excluded from Silver and Gold.

---

## Scenario 5: SCD Key Explosion

**What it injects:** Generates 800 customer profile update events in a single day.
Normal volume is 5-15/day. The updates are valid — just abnormally voluminous.

**Why this is interesting:** There is no pipeline failure. The data is correct.
But Gold's `dim_customer` SCD Type 2 job, which normally processes 10-15 updates
in 8 minutes, now processes 800 and takes 45 minutes. The Databricks job
still succeeds — but the run timestamp shows a 5x latency spike.

### Commands

```bash
# Inject
python main.py --date 2025-01-15 --failure scd_key_explosion

# Verify — check customer update count for this date
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT COUNT(*) FROM velora_crm.customers WHERE updated_at >= '2025-01-15'"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds |
| Bronze | Succeeds |
| Silver | Succeeds — 800 customer updates merged |
| Gold | Succeeds — but dim_customer job takes 45min vs 8min normally |

### Expected PipelineIQ response

- Detection: Gold job duration anomaly (latency 5x above baseline)
- No error — detection is purely temporal
- RCA summary: *"Gold dim_customer job runtime spiked from ~8 minutes to ~45 minutes. 800 SCD Type 2 customer update events were processed versus the normal 5-15. This is consistent with a bulk CRM migration or data correction batch."*
- Affected component: `gold.dim_customer`

### Reset

No action needed. The 800 SCD rows are correct data.

---

## Scenario 6: Dependency Violation

**What it injects:** Sets a `force_early_fact_run` flag in `velora_oms.control_flags`.
The ADF pipeline is configured to check this flag — if set, it triggers `fact_order_line`
immediately after `orders` loads, before `dim_product` has finished refreshing.

**Why this breaks things:** `fact_order_line` performs a surrogate key lookup against
`dim_product` to resolve `product_surrogate_key`. If `dim_product` hasn't finished
refreshing, the lookup finds no current rows and returns NULL surrogate keys for all
products. The fact table writes successfully but every `product_surrogate_key` is NULL.

### Commands

```bash
# Inject (flag is written to control_flags table)
python main.py --date 2025-01-15 --failure dependency_violation

# Verify the flag exists
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "SELECT * FROM velora_oms.control_flags WHERE flag_name = 'force_early_fact_run'"
```

### Expected pipeline behaviour

| Layer | Outcome |
|---|---|
| ADF Copy | Succeeds |
| Bronze | Succeeds |
| Silver | Succeeds |
| Gold | dim_product: not yet complete; fact_order_line: runs early, all product_surrogate_key = NULL |

### Expected PipelineIQ response

- Detection: NULL surrogate key rate anomaly in fact_order_line
- RCA summary: *"fact_order_line loaded before dim_product refresh completed. All product_surrogate_key values in today's fact load are NULL. Check ADF pipeline dependency configuration — ensure dim_product activity completes before fact_order_line activity starts."*
- Affected component: `gold.fact_order_line`

### Reset

```bash
# Clear the control flag
sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD \
  -Q "DELETE FROM velora_oms.control_flags WHERE flag_name = 'force_early_fact_run'"

# Re-run Gold to correct the NULL surrogate keys
# (The next normal pipeline run will overwrite today's fact data with correct surrogate keys
#  if the ADF pipeline uses MERGE on order_line_id)
```

---

## Running all 6 scenarios for a demo

For a complete PipelineIQ demo, inject each scenario in sequence over 6 dates:

```bash
python main.py --date 2025-01-15 --failure schema_drift
python main.py --date 2025-01-16 --failure referential_integrity
python main.py --date 2025-01-17 --failure volume_anomaly
python main.py --date 2025-01-18 --failure null_constraint
python main.py --date 2025-01-19 --failure scd_key_explosion
python main.py --date 2025-01-20 --failure dependency_violation
```

Each injection produces a distinct incident in `pipelineiq.incident_store` with a
different root cause and a different piece of IaC evidence retrieved by pgvector.
