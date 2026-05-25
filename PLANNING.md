# PipelineIQ — Planning

## Contents

- [Client: Velora Retail Group](#client-velora-retail-group) — narrative + the problem this solves
- [Full stack](#full-stack) — every Azure service + role + tier
- [Dataset and data generation](#dataset-and-data-generation) — generator design + source tables + daily volumes
- [ADF pipeline design](#adf-pipeline-design) — one parameterised pipeline for all 10 source tables
- [PostgreSQL control plane tables](#postgresql-control-plane-tables) — operational + observability schemas
- [Gold data model — star schema](#gold-data-model--star-schema) — 9 dims + 3 facts
- [Observability failure scenarios (6 classes)](#observability-failure-scenarios-6-classes) — what PipelineIQ detects + diagnoses
- [Modular architecture](#modular-architecture) — Azure-native, swap-ready by docs
- [Cost estimate (Central India, 1 USD = Rs. 95, PAYG)](#cost-estimate-central-india-1-usd--rs-95-payg) — ~Rs. 5K/month
- [Hard constraints](#hard-constraints) — non-negotiables
- [Phase-by-phase exit criteria](#phase-by-phase-exit-criteria) — what "done" means for each phase
- [Phase dependencies](#phase-dependencies) — what blocks what, what can run in parallel

## Client: Velora Retail Group

Mid-market omnichannel retailer, Bangalore-headquartered.
Revenue ~Rs.850 crore/year. Three channels: D2C ecommerce,
B2B wholesale (200+ reseller partners), 45 physical stores
across 8 Indian cities. 4,200 active SKUs across 5 divisions:
Consumer Electronics, Home Appliances, Personal Care,
Sports & Fitness, Premium Accessories.

**The problem PipelineIQ solves:**
Nightly pipelines must deliver a unified sales and inventory
view by 7am. When they fail, engineers spend 2-3 hours
diagnosing manually without architectural context. Target
MTTR with PipelineIQ: under 15 minutes.

---

## Full stack

| Service | Role | Tier / config |
|---|---|---|
| Azure SQL Database | Source system | Serverless, auto-pause 60min, max 2 vCores |
| Azure Data Factory | Orchestration only — moves data, triggers compute | Standard |
| ADLS Gen2 | Central storage — all Delta tables, raw files | LRS, hierarchical namespace |
| Databricks Premium | ETL (Jobs Compute) + SQL serving (SQL Warehouse) | Premium required |
| PostgreSQL + pgvector | Control DB + IaC vector store — dual purpose | B2s Flexible Server |
| Azure Functions | Control plane API — wraps PostgreSQL as REST | Python 3.11, consumption |
| Azure Monitor + Log Analytics | Telemetry aggregation — KQL queryable | Standard |
| FastAPI | PipelineIQ backend — orchestrates RCA loop | Container Apps, scale to zero |
| React | Dashboard frontend | Static Web Apps, free tier |
| Key Vault | Secret management | All services use managed identity |
| Azure DevOps | Source control + CI | pipelineiq-iac + pipelineiq-app repos |
| AI model | OpenAI GPT-4o via Azure OpenAI Service | Standard deployment |

Region: Central India
Currency: 1 USD = Rs. 95

---

## Dataset and data generation

Single synthetic dataset only. Velora Retail Group.
No public datasets. NYC Taxi and Chicago Taxi are dropped.

**Generator:** Python (Faker + NumPy) as Azure Function on **FC1 Flex Consumption**
(see DECISIONS #50 — Y1 Linux Consumption was unreliable for timer triggers).
Runs at 06:00 UTC daily and writes "yesterday's batch" — `today_utc - 1` —
to Azure SQL (DECISIONS #51, supersedes #49's narrative-ordinal model).
Has an idempotency guard at the top of `run()` that no-ops cleanly when
the target date already has data, so manual backfills + scheduled fires
coexist without PK collisions. Manual backfill convention: stops at
`today - 1` (leave today's date for the Function). Has failure_injector
flag for all 6 failure classes. Uses seed=42 + date-derived offset
(DECISIONS #41) for deterministic per-date output.

**Generator modules:**
- config.py — volume params, failure mode toggle, date rules
- catalogue.py — static product, territory, category master data
- customers.py — customer pool, grows daily, SCD Type 2 events
- orders.py — daily order generation with realistic distributions
- status_updates.py — order status progression logic
- dimension_changes.py — price changes, rep reassignments
- failure_injector.py — controlled bad data for all 6 scenarios
- main.py — assembles all sections, writes to Azure SQL

**Daily volumes:**
- New D2C orders: 150-300/day (INSERT)
- New B2B wholesale orders: 20-50/day (INSERT)
- New store POS orders: 80-150/day (INSERT)
- Order status progressions: 200-400/day (MERGE UPDATE)
- New customer registrations: 15-40/day (INSERT)
- Customer profile updates: 5-15/day (SCD Type 2)
- Inventory snapshot: all 4,200 SKUs all 45 stores (FULL REFRESH daily)
- Product price changes: 3-8/week every Monday (SCD Type 2)
- New product launches: 5-10/month (INSERT)
- Sales rep territory changes: 1-2/quarter (SCD Type 2)

**Source tables in Azure SQL (10 tables, 4 schemas):**

velora_oms:
  orders             — order_id, customer_id, channel_type, order_date, status, created_at, updated_at
  order_lines        — line_id, order_id, product_id, quantity, unit_price, discount_amt, created_at, updated_at
  order_status_log   — log_id, order_id, from_status, to_status, changed_at, created_at

velora_crm:
  customers          — customer_id, full_name, email, segment, city, account_type, created_at, updated_at
  customer_addresses — address_id, customer_id, address_line, city, pincode, is_primary, created_at, updated_at

velora_pim:
  products           — product_id, sku, product_name, category_id, division, is_active, created_at, updated_at
  product_pricing    — pricing_id, product_id, list_price, effective_from, effective_to, created_at
  inventory_snapshot — snapshot_id, product_id, store_id, opening_stock, closing_stock, stockout_flag, snapshot_date

velora_hrm:
  sales_reps         — rep_id, full_name, email, hire_date, is_active, created_at, updated_at
  territory_assignments — assignment_id, rep_id, territory_id, assigned_from, assigned_to, created_at

Every table carries: created_at, updated_at, source_system (audit columns).
ADF uses updated_at as watermark column for incremental extraction.

---

## ADF pipeline design

One parameterised ADF pipeline handles all 10 source tables.
Parameters: schema_name, table_name, watermark_column, load_type.
Config driven by PostgreSQL entity_registry table.
Adding a source = one INSERT into entity_registry, no ADF changes.

ADF flow per entity:
1. Call Function: get_watermark(entity_name)
2. Copy Activity: SELECT * FROM {schema}.{table} WHERE updated_at > watermark
3. Write Parquet to ADLS landing/{entity}/{date}/
4. Call Function: register_file(entity, path, row_count)
5. Trigger Databricks Job for this entity
6. On success: Call Function: commit_watermark(entity, new_watermark)

Watermark only advances on confirmed pipeline success. Failure
leaves watermark unchanged so next run retries same window.

---

## PostgreSQL control plane tables

Schema: pipeline (operational) + pipelineiq (observability)

pipeline.entity_registry   — entity_name, source_schema, source_table,
                             watermark_column, load_type, schedule,
                             active, priority, depends_on

pipeline.watermarks        — entity_name, environment,
                             last_successful_load, next_window_start

pipeline.process_queue     — entity_name, scheduled_run_time,
                             status (pending/running/complete/failed),
                             retry_count, last_heartbeat

pipeline.file_registry     — file_path, source_entity, landed_at,
                             row_count, pipeline_run_id, processed_flag

pipeline.pipeline_exec_log — run_id, pipeline_name, entity_name,
                             start_time, end_time, status,
                             rows_read, rows_written, rows_rejected,
                             error_message

pipelineiq.incident_store  — incident_id, pipeline_id, failure_timestamp,
                             root_cause_summary, affected_component,
                             evidence, suggested_fix, confidence,
                             iac_chunks_used, raw_logs_used, created_at
                             (append-only, never UPDATE or DELETE)

pipelineiq.iac_embeddings  — id, file_path, resource_type, branch,
                             content, embedding vector(1536), ingested_at
                             (cosine similarity index on embedding)

---

## Gold data model — star schema

### Dimensions (9)

dim_customer        SCD Type 2 — tracks segment and city changes
                    surrogate_key, customer_id, full_name, email,
                    segment, city, account_type, channel_type,
                    valid_from, valid_to, is_current

dim_product         SCD Type 2 on list_price only
                    surrogate_key, product_id, sku, product_name,
                    division, is_active, list_price,
                    valid_from, valid_to, is_current

dim_product_category SCD Type 0 — static
                    category_id, category_name, division

dim_sales_channel   SCD Type 0 — static (D2C / B2B / Store)
                    channel_id, channel_name, channel_type

dim_sales_rep       SCD Type 2 — tracks territory reassignment
                    surrogate_key, rep_id, full_name, email,
                    territory_id, is_active,
                    valid_from, valid_to, is_current

dim_territory       SCD Type 1 — 8 cities, rarely changes
                    territory_id, territory_name, city, region

dim_order_status    SCD Type 0 — static lookup
                    status_id, status_name, status_category

dim_date            SCD Type 0 — calendar 2020-2030
                    date_id, full_date, day_of_week, is_weekend,
                    month_name, quarter, fiscal_year, is_public_holiday

dim_store           SCD Type 1 — 45 Velora stores
                    store_id, store_name, city, store_tier,
                    territory_id, is_active

### Facts (3)

fact_order_line
  Grain: one SKU on one order
  order_line_id, order_id, order_date_id, customer_surrogate_key,
  product_surrogate_key, channel_id, rep_surrogate_key,
  store_id, territory_id, status_id,
  quantity_ordered, unit_price_at_sale, discount_amount,
  line_total_inr, tax_amount, net_revenue_inr,
  _pipeline_run_id, _ingestion_timestamp

fact_daily_channel_revenue
  Grain: channel + product_category + date
  Pre-aggregated for BI dashboards
  date_id, channel_id, category_id, territory_id,
  total_orders, total_units_sold, gross_revenue_inr,
  net_revenue_inr, avg_order_value_inr, return_rate_pct

fact_inventory_daily
  Grain: product + store + date
  date_id, product_surrogate_key, store_id,
  opening_stock, units_sold, units_returned,
  closing_stock, stockout_flag

### Quarantine (2)

quarantine.orders      — rejected order records with rejection_reason + pipeline_run_id
quarantine.order_lines — rejected order line records with same

---

## Observability failure scenarios (6 classes)

All injected via failure_injector flag in generator.

1. Schema drift
   Injection: generator adds promo_code VARCHAR(20) to orders
   Bronze: loads fine (schema-on-read)
   Silver: MERGE fails on schema mismatch
   RCA: "New source column not in Silver schema. Enable schema
         evolution or add column explicitly to Silver definition."

2. Referential integrity
   Injection: 30 order lines with product_id = 9999 (does not exist)
   Silver: DQ check catches, quarantines with UNKNOWN_PRODUCT_ID
   RCA: "Orphaned foreign key on product_id. Product master not
         synced for new SKUs introduced today."

3. Volume anomaly
   Injection: 12 orders instead of normal 300 (POS outage simulation)
   All jobs: succeed — no notebook error
   Detection: row count 96% below 7-day rolling average
   RCA: "Volume alert fired despite successful run. Flags silent data
         loss — invisible to traditional monitoring."

4. Null constraint
   Injection: 50 order lines with null unit_price
   Silver: DQ rejects to quarantine
   RCA: "Null in non-nullable price column. Pricing lookup returned
         no match for SKUs launched today."

5. SCD key explosion
   Injection: 800 customer profile updates (CRM bulk migration)
   Gold: 800 new SCD Type 2 rows, latency spikes from 8min to 45min
   RCA: "Unusual SCD Type 2 volume caused latency spike. Investigate
         upstream CRM batch job."

6. Dependency violation
   Injection: fact_order_line triggered before dim_product finishes
   Gold: FK lookups return null surrogate keys
   RCA: "Gold fact loaded before dimension refresh completed.
         Dependency ordering violated in ADF pipeline."

---

## Modular architecture

Azure-native. Swap-ready by documentation, not abstraction.
Each Terraform module README documents AWS and GCP equivalents.

New client onboarding:
1. Create clients/{client_name}/main.tf + variables.tf
2. terraform apply
3. INSERT all source entities into pipeline.entity_registry
4. PipelineIQ observability layer works immediately

No changes to core/, no changes to pipelineiq_app/.

---

## Cost estimate (Central India, 1 USD = Rs. 95, PAYG)

| Service | Rs./month | Rs. 4 months |
|---|---|---|
| Databricks Jobs Compute (DBU + VM) | Rs. 1,406 | Rs. 5,624 |
| Databricks All-Purpose (DBU + VM) | Rs. 902 | Rs. 3,607 |
| Databricks SQL Warehouse | Rs. 251 | Rs. 1,003 |
| Azure SQL Database | Rs. 380 | Rs. 1,520 |
| Azure Data Factory | Rs. 736 | Rs. 2,945 |
| ADLS Gen2 | Rs. 57 | Rs. 228 |
| PostgreSQL Flexible Server | Rs. 950 | Rs. 3,800 |
| Azure OpenAI (GPT-4o) | Rs. 103 | Rs. 410 |
| Embeddings | Rs. 1 | Rs. 4 |
| Azure Monitor + Log Analytics | Rs. 219 | Rs. 874 |
| Container Apps (FastAPI) | Rs. 238 | Rs. 950 |
| Functions, Static Web Apps, Key Vault, DevOps | Rs. 0 | Rs. 0 |
| **TOTAL** | **~Rs. 5,243** | **~Rs. 20,970** |

Budget: Rs. 22,000 for 4 months.
Stop PostgreSQL nights/weekends to save ~Rs. 300/month.
Databricks All-Purpose 30-min auto-terminate is non-negotiable.

---

## Hard constraints

- AI reads logs and metadata only. Never touches production data.
- IaC on main branch is the only architectural source of truth.
- PRs never auto-merged to main. Staging only, human approval required.
- Every AI action logged immutably with full reasoning chain.
- Logs pre-filtered by severity + pipeline ID before any LLM call.
- Watermarks advance only on confirmed pipeline success.
- Nothing hardcoded. All secrets in Key Vault via managed identity.
- Unity Catalog enabled on Databricks from day one.
- Databricks All-Purpose clusters always have 30-min auto-terminate.
- PostgreSQL incident_store and pipeline_exec_log are append-only.

---

## Phase-by-phase exit criteria

Each phase is "done" only when every bullet below is true. No fuzzy
exits — if you can't tick all the boxes, the phase isn't done. Use
`docs/build_order.md` for resource-level status; this section is for
phase-level "ship/no-ship" gates.

### Phase 0 — Foundations
Done when:
- All Tier 0–5 items in `docs/build_order.md` are `Done`.
- 30+ Azure resources live in `pipelineiq-rg-dev` (KV, ADLS, LAW, Postgres, Databricks workspace, UC metastore + catalogs, SQL warehouse, Function App + scheduler, OpenAI + GPT-4o deployment, Azure SQL + bootstrap schema).
- `terraform plan` from `clients/velora/` is clean.

### Phase 1 — Data generator
Done when:
- Function App fires daily at 00:30 UTC via Logic App. **Inventory write owned by Databricks Job at 00:35 UTC** (S14, DECISIONS #71).
- `velora_oms` has ≥14 consecutive days of activity. Each day: ~300-500 orders, ~1200-1700 order_lines, ~1000-1300 status_log rows, 189,225 inventory_snapshot rows.
- Idempotency guard prevents double-fire collisions. Generator runs in <2 min wall (Function) + ~5 min wall (Databricks Job).
- All 6 failure-injector flags exercised once via `python generator/main.py --failure <class>` (output verified, downstream impact deferred to Phase 3).

### Phase 2 — Medallion (landing → bronze → silver → gold)
Done when:
- All 12 entities ingested through gold. silver 10/10, gold 12/12 (9 dims + 3 facts).
- 0 DQ rejects on real-dated generator output. Quarantine path wired for every silver notebook (exercised via Phase 3 failure injection).
- silver↔gold reconciliation passes on row count + key measures.
- dim_customer SCD-2: 0 surrogate-key collisions (post-DECISIONS #68 bulletproof fix).
- ADF replacement for `scripts/export_velora_to_landing.py` ships in Phase 0 Tier 6.

### Phase 3 — Failure injection + RCA loop
Done when:
- All 6 failure scenarios injectable via flag; each produces a row in `pipelineiq.incident_store` within 5 min of detection.
- Incident rows have: root_cause_summary (1-2 sentences), affected_component, evidence (raw log lines + IaC chunks used), suggested_fix, confidence (0-1).
- Slack webhook fires for severity ≥ medium.
- KQL query on Azure Monitor catches ADF + Databricks job failures and routes to FastAPI within 60s.
- **Blocker:** requires Phase 0 Tier 6 (ADF + `pipeline_exec_log`) — Phase 3 reads failure signals from there.

### Phase 4 — pgvector IaC embeddings
Done when:
- Every .tf and .bicep file in `PipelineIQ-IaC/main` is chunked, embedded, and stored in `pipelineiq.iac_embeddings`.
- Azure DevOps webhook on push to `main` re-chunks changed files within 60s.
- Phase 3 RCA loop retrieves top-K chunks via cosine similarity (typical K=5, threshold >0.7).
- pgvector ivfflat index returns results in <100ms p99.
- **Can run in parallel with Tier 6 / Phase 3** — no dependencies between them.

### Phase 5 — FastAPI backend on Container Apps
Done when:
- FastAPI deployed to `pipelineiq-fastapi-dev` on Container Apps (scale-to-zero).
- REST endpoints live: `/v1/incidents`, `/v1/pipelines/{run_id}/status`, `/v1/iac/chunks`, `/v1/webhooks/iac`.
- Internal KQL polling cron runs every 60s; on new failure → triggers RCA loop → writes incident → fires Slack.
- Authentication: function-key / API key for service-to-service, AAD (optional) for human callers.
- Logs stream to `pipelineiq-logs-dev`.

### Phase 6 — React dashboard
Done when:
- React app deployed to Static Web Apps (free tier).
- Three views render: live pipeline status, incident timeline, per-incident RCA detail.
- Each incident row links to the IaC chunks used + raw log excerpt + suggested fix (Markdown render).
- AAD auth wired (optional for dev).
- End-to-end demo: trigger a failure scenario via flag → see the incident appear in the timeline within 5 min → Slack alert fires with link back to the React UI.

---

## Phase dependencies

```
Phase 0 (Foundations)
   │
   └── Phase 1 (Generator) ───┐
                              │
   └── Tier 6 (ADF + metadata)┼─── Phase 3 (RCA loop)
                              │
   └── Phase 4 (pgvector) ────┘
                              │
                              └── Phase 5 (FastAPI)
                                       │
                                       └── Phase 6 (React)
```

Reading the graph:
- Phase 2 (Medallion) runs alongside Phase 1 — both consume Phase 0 only.
- **Tier 6 (ADF + `pipeline.*` activation) is the bottleneck** — it unlocks Phase 3 by providing real `pipeline_exec_log` failure signals.
- **Phase 4 (pgvector) is independent of Tier 6** — can interleave to save context-switching cost.
- Phase 3 needs both Tier 6 (signals) AND Phase 4 (retrieval context) to be honest. Either can land first; Phase 3 finishes second.
- Phase 5 (FastAPI) orchestrates Phase 3's loop — needs Phase 3's code paths to exist (even if not yet end-to-end verified).
- Phase 6 (React) consumes Phase 5's REST endpoints — last in the chain.

See `docs/forward_plan.md` for the session-by-session sequencing this dependency graph implies.
