# PipelineIQ — Architecture Contract

This file is the single source of truth for any Claude Code session
working on this project. Read this file first, every session, no
exceptions. Then read only the files explicitly listed for your task.

---

## What this project is

PipelineIQ is an AI-native pipeline observability and DevOps co-pilot.
It monitors Azure data pipelines in real time. When a pipeline fails,
it reads the IaC from the main branch, performs AI-driven root cause
analysis, writes a plain English incident summary, fires a Slack alert,
and guides the engineer through a permission-gated fix workflow.

Client: Velora Retail Group (synthetic enterprise dataset).
The AI never acts without human approval at every step.

---

## Repo map

```
pipelineiq-iac/               Terraform + Bicep for all Azure resources
  core/                       Core infrastructure modules (never touch without reason)
  source_connectors/
    azure_sql/                ADF linked service + parameterised dataset for Velora
    blob_storage/             For file-drop source clients
    http_api/                 For REST API source clients
    eventhub/                 For streaming source clients
  clients/
    velora/                   Velora-specific config (main.tf + variables.tf only)
  pipelineiq_app/             ADF pipelines, notebooks, FastAPI, React Terraform

pipelineiq-app/               Application code
  CLAUDE.md                   This file — read first every session
  PLANNING.md                 Full architecture, stack decisions, constraints
  PROGRESS.md                 Current build status and next task
  SCHEMA.md                   Full data model — read before any data/SQL work
  DECISIONS.md                Running log of architectural choices and rationale
  docs/                       Phase-by-phase documentation (written after building)
    architecture.md
    data_generation.md
    pipeline.md
    observability.md
    ai_rca.md
    api.md
    dashboard.md
    runbooks/
      start_stop_postgres.md
      inject_failure.md
      new_client_onboarding.md
  generator/                  Velora synthetic data generator
    config.py
    catalogue.py
    customers.py
    orders.py
    status_updates.py
    dimension_changes.py
    failure_injector.py
    main.py
  notebooks/                  Databricks PySpark notebooks
    bronze/
    silver/
    gold/
  functions/                  Azure Functions (Python) — control plane API
  fastapi/                    PipelineIQ FastAPI backend
  react/                      React dashboard frontend
  scripts/                    Bootstrap and utility scripts
    bootstrap_state.sh        Run once to create Terraform state backend
    bootstrap_postgres.sql    Creates all PostgreSQL control plane tables
    bootstrap_sql.sql         Creates all Azure SQL Velora source tables
```

---

## Tech stack and versions

| Component | Technology | Version / tier |
|---|---|---|
| Orchestration | Azure Data Factory | Standard |
| Source DB | Azure SQL Database | Serverless, max 2 vCores |
| Storage | ADLS Gen2 | LRS, hierarchical namespace |
| ETL compute | Azure Databricks | Premium, Jobs Compute |
| SQL serving | Databricks SQL Warehouse | 2X-Small Classic |
| Control DB | PostgreSQL Flexible Server | B2s + pgvector extension |
| Control API | Azure Functions | Python 3.11, consumption plan |
| AI model | Azure OpenAI GPT-4o | Via Azure OpenAI Service |
| Backend | FastAPI | Python 3.11, Container Apps |
| Frontend | React | Static Web Apps, free tier |
| Observability | Azure Monitor + Log Analytics | Standard |
| IaC | Terraform | >= 1.6, azurerm >= 3.90 |
| IaC (ADF) | Bicep | Latest |
| Python | Generator + Functions + FastAPI | 3.11 |
| PySpark | Databricks notebooks | DBR 14.x LTS |

---

## Medallion layer boundaries

```
landing/    Raw Parquet files exactly as extracted from Azure SQL.
            No transformation. No compute. File registry only.
            Owner: ADF Copy Activity.

bronze/     Schema enforced. Audit columns appended. Append-only Delta.
            No business logic. No deduplication.
            Columns added: _source_file, _ingestion_timestamp, _pipeline_run_id
            Owner: Bronze Databricks notebook.

silver/     Deduplicated. DQ-validated. MERGE on business key.
            Three channels unified under channel_type.
            Bad records quarantined with rejection_reason + pipeline_run_id.
            Owner: Silver Databricks notebook.

gold/       Warehouse-shaped star schema. SCD logic applied.
            Surrogate keys assigned. Pre-aggregated facts built.
            External tables in Unity Catalog pointing at ADLS paths.
            Owner: Gold dimension and fact Databricks notebooks.

quarantine/ Bad records from Silver DQ checks.
            Never deleted. Append-only.
            Contains: rejection_reason, pipeline_run_id, raw record.
```

**Layer crossing rule:** data only flows forward (landing → bronze →
silver → gold). No notebook reads from a downstream layer. No notebook
writes to an upstream layer. Gold notebooks read from silver only.

---

## Azure service topology

```
Azure SQL Database (serverless)
  └── ADF Copy Activity (watermark-based incremental)
        └── ADLS Gen2 landing/
              └── Bronze Databricks Job
                    └── ADLS Gen2 bronze/ (Delta)
                          └── Silver Databricks Job
                                └── ADLS Gen2 silver/ (Delta)
                                      └── Gold Databricks Jobs
                                            └── ADLS Gen2 gold/ (Delta)
                                                  └── Databricks SQL Warehouse
                                                        (JDBC/ODBC → VS Code, Power BI)

PostgreSQL Flexible Server
  ├── Relational: entity_registry, watermarks, process_queue,
  │              file_registry, pipeline_exec_log, incident_store
  └── pgvector:  iac_embeddings (cosine similarity search)

Azure DevOps (pipelineiq-iac)
  └── IaC push to main
        └── FastAPI webhook → IaC chunker → pgvector upsert

Azure Monitor + Log Analytics
  ├── ADF diagnostic logs
  ├── Databricks diagnostic logs
  └── Container Apps logs
        └── FastAPI KQL polling → PostgreSQL failure_events
              └── pgvector retrieval + Claude Sonnet 4.6
                    └── PostgreSQL incident_store + Slack webhook
```

---

## Naming convention

All Azure resources: `pipelineiq-{component}-{environment}`
Examples: `pipelineiq-databricks-dev`, `pipelineiq-adls-dev`

All Terraform variables: snake_case
All Python: snake_case, PySpark DataFrames: df_{layer}_{entity}
All SQL: snake_case, no reserved words as column names
All Delta tables: {layer}.{entity} e.g. `bronze.orders`, `gold.dim_customer`
All Azure Functions: verb_noun e.g. `get_watermark`, `register_file`
All FastAPI routes: /v1/{resource}/{action}

Required tags on every Azure resource:
```
project     = "pipelineiq"
environment = var.environment
owner       = "data-engineering"
managed_by  = "terraform"
```

---

## Module stability

| Module | Status | Notes |
|---|---|---|
| core/ | Stable once deployed | Do not modify without explicit instruction |
| source_connectors/azure_sql/ | Stable | Velora-specific connector |
| clients/velora/ | Config only | Safe to update variables |
| generator/ | **Real-dated + guarded (S6) + 40613-resilient (S11)** | DECISIONS #51: `yesterday_utc()` + idempotency guard. DECISIONS #61 (S11): `_connect_with_resume_retry` retries Azure SQL serverless wake-up errors. DECISIONS #62 (S11): chunked inventory write w/ per-chunk commits. Source DB has 14 real-dated days Apr 27 → May 10, 2026. Manual backfills must stop at `today-1`; re-seed of an already-populated date requires explicit wipe. `scripts/inventory_only.py --date YYYY-MM-DD` is the laptop fallback for inventory-only writes when the function fire dropped inventory. |
| notebooks/bronze/ | **All 12 entities hydrated (S7 + S9.5)** | `ingest_to_bronze.py` is entity-agnostic (DECISIONS #48). 10 main entities (~1.9M rows) + 2 static seeds added in S9.5: `bronze.default.product_categories` (35 rows) + `bronze.default.stores` (45 rows). DECISIONS #60. |
| notebooks/silver/ | **7/10 tables done (S8 + S9.5).** | `silver.orders` (3,619), `silver.customers` (248), `silver.order_lines` (12,300), `silver.products` (4,205), `silver.product_pricing` (4,218), `silver.sales_reps` (30), `silver.territory_assignments` (30). All 100% DQ pass on real-dated source. Remaining 3: `silver.inventory_snapshot`, `silver.order_status_log`, `silver.customer_addresses` (chunk 4). |
| notebooks/gold/ | **11/12 dims+facts done (S8 + S9.5 + S10).** | All 8 dims live (S8/S9.5/S10). **S10 chunk 3:** `fact_order_line` (12,300 rows = silver.order_lines 1:1, as-of joins to 3 SCD-2 dims with floor-at-earliest fallback, per-channel territory derivation, GST 18% tax, MERGE on `line_id`). `fact_daily_channel_revenue` (Gold→Gold rollup at (date_id, channel_id, category_id, territory_id) grain; units + net_revenue reconcile fact↔rollup exactly). Remaining 1: `fact_inventory_daily` (chunk 4, paired with `silver.inventory_snapshot`). |
| functions/ | **Stable on FC1 Flex + Logic-App-driven schedule + S11 cold-start + inventory fixes** | Migrated Y1 → FC1 Flex Consumption (DECISIONS #50). **S9 (DECISIONS #59):** Daily fire moved to a Logic App (`pipelineiq-scheduler-dev`, `core/scheduler/` IaC) that POSTs to `/admin/functions/generator` at 00:30 UTC. Function `host.json` `functionTimeout` bumped 10m → 30m. **S11 (DECISIONS #61 + #62):** `_connect_with_resume_retry` wraps `pyodbc.connect` to retry on Azure SQL 40613 / HYT00 (cold-DB wake-up); inventory write switched to chunked-per-chunk-commit (10K rows/chunk) with progress logging — both addressing two distinct silent-failure modes that took down 5/10 + 5/11 autonomous fires. Function timer remains registered as harmless fallback (idempotency guard from #51 makes double-fire safe). ADF replacements for Bronze chain still pending in Tier 6. |
| scheduler/ (IaC `core/scheduler/`) | **Live (S9)** | Logic App Consumption recurrence trigger (`daily-fire`, 00:30 UTC) → HTTP action POSTing to the Function App admin endpoint with `x-functions-key` from `azurerm_function_app_host_keys.primary_key`. Single source of truth for the daily generator schedule. Cost: effectively Rs.0/mo (1 fire/day << 4,000-action free grant). |
| fastapi/ | Pending | Phase 5 |
| react/ | Pending | Phase 6 |
| IaC core modules (keyvault, log_analytics, adls, postgres, databricks, openai) | Stable | Phase 0 — all applied |
| IaC source_connectors/azure_sql | Stable | Phase 0 — applied |
| IaC core/databricks_uc | **Stable (S8)** | Adopted system metastore + **4 catalogs** (`bronze/silver/gold/quarantine`) + 5 external locations + storage credential + cluster policy + SQL warehouse + secret scope. DECISIONS #46. `quarantine` catalog added in S8 (variable default updated 3→4). |
| scripts/export_velora_to_landing.py | Stable (scaffold) | Phase 2 — substitutes for ADF until Tier 6 lands. DECISIONS #47. |
| scripts/run_bronze_smoke.py | Stable (scaffold) | Phase 2 — driver for one-off Bronze ingestion. Replaced by ADF + scheduled Job in Tier 6. |

---

## Standard commands

```bash
# Terraform — run from pipelineiq-iac/clients/velora/
terraform fmt -recursive          # format before every commit
terraform validate                # validate before plan
terraform plan -out=tfplan        # always save plan output
terraform apply tfplan            # apply saved plan only

# Bootstrap (run once, before any terraform)
bash scripts/bootstrap_state.sh   # creates Terraform state backend

# PostgreSQL bootstrap (run once after PostgreSQL provisioned)
psql $POSTGRES_URL -f scripts/bootstrap_postgres.sql

# Azure SQL bootstrap (run once after Azure SQL provisioned)
sqlcmd -S $SQL_SERVER -d $SQL_DB -i scripts/bootstrap_sql.sql

# Generator (local test)
cd generator && python main.py --date 2025-01-15 --dry-run

# Generator (inject failure)
cd generator && python main.py --date 2025-01-15 --failure schema_drift

# FastAPI (local)
cd fastapi && uvicorn main:app --reload --port 8000

# Databricks notebook test (via CLI)
databricks jobs run-now --job-id {job_id}

# Refresh source-system firewall to current public IP
# (run when velora_oms / Azure SQL connections fail with firewall errors,
#  or when the user says "update ip firewall at source")
bash scripts/update_sql_firewall_ip.sh
```

---

## Azure subscription — non-negotiable default

**Every Azure resource in this project lives on `Microsoft Azure Sponsorship`**
(subscription id `ea05f17f-b2bb-40ac-a391-afe41a9f5cbf`, tenant
`23d48723-f83f-4245-9aec-192176d3b96c` = `SailAnalyticsAP.onmicrosoft.com`).
That includes `velora_oms` (Azure SQL), the Function App
(`pipelineiq-functions-dev`), ADLS, Databricks, Key Vault, Postgres, the Portal
Azure OpenAI resource (`pipeline-iq-resource`) — everything. The user also has a
separate `SSE BI Subscription` on a different tenant (`2278c488-...`) for
unrelated work.

**Rule:** Before *any* `az` CLI call, `pyodbc`/`DefaultAzureCredential` against
`velora_oms`, or terraform run touching this project, ensure the active sub is
Sponsorship. `az account set` is global to `~/.azure/`, so it stays sticky once
set — but verify before assuming. Symptoms of being on the wrong sub:
`ResourceGroupNotFound: pipelineiq-rg-dev`, `Login failed for user
'<token-identified principal>'. The server is not currently configured to
accept this token` (= AAD token issued for the wrong tenant).

```bash
# Always run at session start (idempotent, no-op if already set):
az account set --subscription "Microsoft Azure Sponsorship"
az account show --query "[name,tenantId]" -o tsv  # verify
```

---

## Source-system access (firewall auto-recover)

The velora source DB (`pipelineiq-sql-velora-dev` / `velora_oms`) is fronted by an
Azure SQL firewall. Two rules grant access:

- `allow-vpn-ip` → `69.5.168.130` (the dedicated Sail VPN IP — stable; covered by
  memory `project_vpn_dedicated_ip.md`).
- `MG-Office-Laptop-Dynamic` → user's current laptop IP (refreshed on demand by
  `scripts/update_sql_firewall_ip.sh`).

**Auto-trigger the script when:**

1. **Any source-system access fails with a firewall-class error** —
   `Cannot open server '...' requested by the login. Client with IP address '...'
   is not allowed to access the server`, or `[HYT00] Login timeout expired` from
   pyodbc / sqlcmd / `az sql db query` against `velora_oms` *when the user is not
   on VPN*. Run the script first, retry the operation, and only escalate if the
   retry still fails.
2. **The user says "update ip firewall at source"** (or close paraphrase like
   "refresh the source firewall", "fix source DB firewall"). Run the script
   without further confirmation — it's idempotent and only touches the
   `MG-Office-Laptop-Dynamic` rule.

The script never touches the VPN rule, so VPN-based access keeps working
regardless. It's safe to run at the start of any session.

---

## Cluster config patterns

**Jobs Compute (ETL notebooks):**
- Node type: Standard_DS3_v2
- Min workers: 1, Max workers: 2
- Auto-terminate: 30 minutes (always)
- Databricks Runtime: 14.x LTS
- Spark config: `spark.sql.extensions io.delta.sql.DeltaSparkSessionExtension`

**All-Purpose Compute (development):**
- Node type: Standard_DS3_v2
- Single node
- Auto-terminate: 30 minutes (non-negotiable)
- Never leave running. Terminate when done.

**SQL Warehouse (serving):**
- Size: 2X-Small
- Auto-stop: 10 minutes
- Type: Classic

---

## Do not touch zones

- `core/` Terraform modules — stable infrastructure, changes require
  explicit instruction and reason
- `_delta_log/` directories in ADLS — never modify manually
- Key Vault secrets — never read or print secret values in code or logs
- PostgreSQL `pipeline_exec_log` and `pipelineiq.incident_store` — append-only,
  never UPDATE or DELETE
- Unity Catalog metastore — managed by Terraform only

---

## Test strategy

**Generator:** Fixed seed (seed=42) produces deterministic output.
Test by comparing row counts and referential integrity checks.

**Notebooks:** Each notebook has a `--dry-run` mode that reads source
data and validates schema without writing. Run before every deploy.

**ADF pipelines:** Use the ADF debug mode with a single-entity run
before triggering full pipeline.

**FastAPI:** pytest with mock PostgreSQL and mock Claude client.
Never call real Claude or real Azure services in unit tests.

**End-to-end:** Inject each of the 6 failure scenarios via
`failure_injector.py` and confirm PipelineIQ produces the expected
Slack alert and incident record within 5 minutes.

---

## Session discipline rules

### During a session

- ONE OBJECTIVE PER SESSION. State it explicitly at the start.
- READ ONLY the files relevant to that objective.
- COMMIT after every meaningful chunk of work.
- USE /compact proactively before context degrades.
- NEVER let a long session be your only record of decisions made.
- UPDATE DECISIONS.md immediately when any architectural choice is made
  (do not defer to end of session — decisions not recorded in the moment are lost).
- UPDATE `docs/build_order.md` status column the moment an item lands
  (`Pending` → `In progress` → `Done` / `Blocked`).

### End of every session (mandatory — no exceptions)

Run this checklist before closing the session. Some items are always-on,
others are conditional — skip only when the condition genuinely does not apply.

| File | When to update | What to write |
|---|---|---|
| **PROGRESS.md** | **Always** | Append a new entry to `## Session Log` using the format below. Update `## Current phase`, `## Next task`, `## Notes and blockers`, and the phase exit criteria tracker if anything moved. |
| **DECISIONS.md** | Whenever any architectural choice was made this session | New numbered row per decision. Never edit or remove prior rows — superseded decisions get a "superseded by #N" note. |
| **`docs/build_order.md`** | Whenever any build-order item changed state | Update status column, date, and path/command for that row. |
| **CLAUDE.md** (this file) | Whenever any new guardrail, convention, do-not-touch zone, standard command, or pending carry-over is worth surfacing to future sessions | Edit the relevant section inline. Pending cross-session items go into `## Pending / carry-overs`. Keep edits surgical — this file is read every session, so every line has to earn its place. |
| **PLANNING.md** | Only when the high-level architecture, stack, or phase plan itself shifted | Edit the affected section. If a decision in DECISIONS.md contradicts PLANNING.md, PLANNING.md must be updated to match. |
| **SCHEMA.md** | Only when the data model changed (new table, new column, type change, constraint change, SCD-type change) | Edit the affected table. Bump any dependent bootstrap SQL (`scripts/bootstrap_sql.sql`, `scripts/bootstrap_postgres.sql`) in the same session. |
| **`docs/{phase}.md`** | Only at the end of a phase (phase exit criteria met) | Write or finalise the phase's doc. Do not write phase docs speculatively before the phase is done. |
| **`docs/runbooks/*.md`** | Whenever an operational procedure was established or changed | Concise, reproducible steps. Commands first, prose second. |
| **`docs/incident_log.md`** | Whenever any blocker, bug, or production-style incident exceeded ~15 min to diagnose or fix | One new entry per incident using the schema at the top of the file. Append only — never edit prior entries. Update the Index table with severity/category/effort/hardest. Fill in Root cause, Fix, and Prevention immediately after the blocker clears — deferring loses the diagnosis detail. |

When the user explicitly says "wrap up the session", "end of session",
"update the docs", or similar: run the full checklist above, no shortcuts.
Treat that phrase as a trigger to audit every file in the table and confirm
each row's status — touched or legitimately skipped.

### Git: commit + push at end of every session (mandatory)

After the docs checklist is satisfied, **commit and push every repo touched
this session** so the session lands as a clean point in history. This is the
guardrail the user explicitly relies on for "go back in time" recovery —
nothing locally-only at end of session.

For each repo with uncommitted work (`PipelineIQ-Architecture`,
`PipelineIQ-IaC`, `PipelineIQ-Portal`):

1. `git status` — confirm only intended files are staged. Never `git add -A`
   without reviewing — `terraform.tfvars`, `.env`, state files, `*.tfplan`,
   key material must stay untracked.
2. One commit per logical chunk; tail commit of the session is the doc-update
   commit (PROGRESS / DECISIONS / build_order / etc.). Reasonable to combine
   docs + last code change if they're tightly coupled.
3. `git push origin main` (or current branch).
4. Verify push succeeded — `git log origin/main..HEAD` should print nothing.

If a push is blocked (auth, branch protection, network), surface the failure
explicitly to the user — do not let the session "end" while changes are still
local. The user's recovery model assumes every session-end has a remote tip.

Never `git push --force` to main without explicit user instruction.

### Session log format

Append to the `## Session Log` section in PROGRESS.md at the end of every session.
Use this exact structure — one to three lines per field, no filler:

```
### {YYYY-MM-DD}
**Objective:** {what the session set out to accomplish}
**Built:** {files created or meaningfully changed — be specific about names}
**Worked:** {what went smoothly, decisions that landed cleanly}
**Broke:** {errors hit, workarounds applied, anything that needed fixing mid-session}
**Uncertainty:** {open questions, things to verify, decisions deferred}
**Next:** {exact first task to pick up in the next session}
**Summary:** {one paragraph — what was achieved, key choices made, note for your future self}
```

The session log is the canonical human record. The memory/ folder holds only what
Claude needs that cannot be derived from reading the project files directly.

---

## Medallion chunk plan (S10 → S13)

**Active multi-session plan. Delete this whole section once chunk 4 lands —
its job is to stay in CLAUDE.md only while it's still open work.**

The remaining 8 Silver tables + 11 Gold dims/facts are split into 4 chunks,
each one session-sized and ending on a deliverable worth committing. Pick
the next un-checked chunk at the start of each session.

### Chunk 1 — broaden silver + claim free static dims  ✅ Done (S9.5, 2026-05-09)
- ✅ `silver.order_lines` (12,300 rows, 100% DQ pass)
- ✅ `silver.products` (4,205 rows, 100% DQ pass)
- ✅ `silver.product_pricing` (4,218 rows, 100% DQ pass)
- ✅ `silver.sales_reps` (30 rows, 100% DQ pass)
- ✅ `silver.territory_assignments` (30 rows, 100% DQ pass)
- ✅ Static-dim batch in `notebooks/gold/build_gold_static_dims.py`:
  `dim_date` (4,018), `dim_sales_channel` (3), `dim_order_status` (6),
  `dim_product_category` (35), `dim_store` (45)
- ✅ Bronze extended for `product_categories` + `stores` (DECISIONS #60)

### Chunk 2 — SCD-2 dims + synthesized dim_territory  ✅ Done (S10, 2026-05-09)
- ✅ `gold.dim_product` (4,218 rows = 4,205 current + 13 historical price-change versions; silver.product_pricing 1:1 match)
- ✅ `gold.dim_sales_rep` (30 rows, all currently active, territory dist matches generator config)
- ✅ `gold.dim_territory` (9 rows = 8 real territories + `D2C_NATIONAL` sentinel)
- ✅ All FK readiness checks clean: dim_store / dim_sales_rep territory_ids → dim_territory; dim_product category_ids → dim_product_category. Verify script: `scripts/verify_gold_chunk2.py`.

**End state:** all 8 dims feeding `fact_order_line` are live.

### Chunk 3 — keystone facts  ✅ Done (S10, 2026-05-09)
- ✅ `gold.fact_order_line` (12,300 rows = silver.order_lines 1:1; 100% as-of-join coverage; all FK + channel-conditional invariants pass)
- ✅ `gold.fact_daily_channel_revenue` (rollup grain = (summary_date_id, channel_id, category_id, territory_id); units + net_revenue reconcile fact↔rollup exactly)
- ✅ Verify script: `scripts/verify_gold_chunk3.py` (~28 checks, all green)

**End state:** revenue analytics queryable end-to-end via the SQL warehouse.

### Chunk 4 — inventory branch + trailing-edge silver  ⏳ Next (start of S11.2)
- [ ] `silver.inventory_snapshot` — 1.89M rows, partition discipline matters.
  Dedup on `(product_id, store_id, snapshot_date)`.
- [ ] `gold.fact_inventory_daily` — as-of join to `dim_product` on
  `snapshot_date`; 7-day trailing avg for `days_of_stock_remaining`.
- [ ] `silver.order_status_log` — no current Gold consumer (placeholder for
  future `fact_order_status_transitions`). Trailing-edge.
- [ ] `silver.customer_addresses` — no current Gold consumer. Trailing-edge.

**End state:** Phase 2 medallion fully complete.

### Reusable patterns (already proven in S8 + S9.5)
- **Silver smoke:** `.venv/bin/python scripts/run_silver_smoke.py --entity {name}`
- **Gold smoke:** `.venv/bin/python scripts/run_gold_smoke.py --entity {name}`
  (entity is the filename suffix after `build_gold_`, e.g. `dim_customer`,
  `static_dims`, `dim_product`, `fact_order_line`)
- Notebook skeletons live in `notebooks/silver/` + `notebooks/gold/` and
  follow the conventions in DECISIONS #52. Copy the closest existing
  notebook and edit the columns + DQ rules / SCD logic.
- After each chunk, run a `verify_*.py` script under `scripts/` for
  end-to-end row-count + key-sanity checks.

### Post-chunk-1 catch-up still owed
Independent of the chunk plan, after tomorrow's autonomous fire verifies:
re-run silver+gold for May-7 / May-8 / May-9 source data via
`run_silver_smoke.py --entity orders|customers|order_lines|products|product_pricing|sales_reps|territory_assignments`
+ `run_gold_smoke.py --entity dim_customer|static_dims`.

---

## Pending / carry-overs

Active items that cross session boundaries. Remove a row once resolved — do
not let this list grow stale. Full context lives in PROGRESS.md `## Session Log`.

- **Generator `--dry-run` mode is broken (known).** Skips catalogue INSERT then
  later calls `pd.read_sql` to load products; gets empty DF and fails in orders
  module with `ValueError: product_pool is empty`. Workaround: skip dry-run and go
  straight to real seed (catalogue is idempotent via UUID5 per DECISIONS #19).
  Proper fix: have dry-run keep the generated catalogue DataFrame in memory and
  short-circuit `pd.read_sql`. Low priority — real seed works end-to-end. Build_order
  item 9.1.
- ~~Tier 4.6 Azure Functions app~~ **Done (S5 addendum, 2026-05-01; migrated to FC1
  Flex Consumption in S6, 2026-05-06).** Function App `pipelineiq-functions-dev`
  live on FC1. See DECISIONS #50 + #51 + `core/functions/` IaC module.
- **App Insights telemetry not flowing on Flex Consumption.** The Function
  executes (proven via `velora_oms` side-effects) but `requests` / `traces` /
  `exceptions` tables in App Insights stay empty. Was also broken on Y1.
  Likely a Flex-specific instrumentation tweak (Python OpenTelemetry mode,
  worker extension, or auto-instrumentation app setting). Investigate when
  observability is needed for Phase 4+ — not blocking Phase 2.
- **Verify tomorrow's autonomous fire under the S11 fixes** (2026-05-12
  00:30 UTC, writes 2026-05-11). S11 caught the 5/10 + 5/11 fires both
  failing at ~15s with SQL 40613 (Azure SQL serverless wake-up) and
  shipped two distinct fixes (DECISIONS #61 + #62): (a) `_connect_with_resume_retry`
  helper retries on 40613 + HYT00 with exponential backoff up to ~5 min;
  (b) inventory write switched to chunked-per-chunk-commit with progress
  logs, after a manual fire on 5/11 wrote orders+lines+status_log then
  silently lost 189K inventory rows when the function host killed the
  Python worker mid-batch. Tomorrow's fire must land 2026-05-11 data in
  **all four** tables. Check via:
  ```sql
  SELECT order_date, COUNT(*) FROM velora_oms.orders WHERE order_date='2026-05-11' GROUP BY order_date;
  SELECT COUNT(*) FROM velora_pim.inventory_snapshot WHERE snapshot_date='2026-05-11';
  SELECT COUNT(*) FROM velora_oms.order_status_log WHERE created_at >= '2026-05-12T00:25:00';
  ```
  If inventory still comes up 0, App Insights will now show exactly which
  chunk it died on (look for `Inventory snapshot: committed N / 189225`
  trace). At that point investigate gRPC host-worker timeout on Flex or
  scale to EP1 Premium for inventory step. Fallback today: `scripts/inventory_only.py
  --date <YYYY-MM-DD>` runs only inventory locally.
  After verifying, **catch up silver/gold** for 2026-05-07 through whatever
  the latest source date is: re-run `export_velora_to_landing.py` +
  bronze ingestion + `run_silver_smoke.py` per entity + `run_gold_smoke.py`
  per dim/fact (idempotent MERGEs everywhere).
- **Tier 6 ADF (Bicep) not yet written.** Linked services (SQL, ADLS, KV, Databricks)
  + parameterised datasets + copy pipeline `velora_oms.*` → `landing/`. Replaces
  `scripts/export_velora_to_landing.py` in production. Not blocking Phase 2 dev —
  the scaffold script is a reasonable substitute until Tier 6 lands.
- **`docs/runbooks/databricks_account_admin_bootstrap.md` step 5 verification path
  is slightly stale.** The runbook says "click avatar → Manage Account should be
  visible" but Databricks's newer UI doesn't surface a literal "Manage Account" link.
  Working verification: open `https://accounts.azuredatabricks.net` directly as the
  promoted user; if the sidebar shows Workspaces / User management / Cloud Resources,
  the role is active. Update the runbook on next touch.
- **Portal Preview + Development env vars** (2026-04-21 S2). Only Production has
  `AZURE_OPENAI_API_KEY` in Vercel — CLI blocked Preview on branch-scoping and
  Development on the `--sensitive` flag. Dashboard overrides both. Not blocking
  the live site; finish via dashboard if/when PR previews or `vercel dev` are
  needed.
- **Repos belong on `mohangowdatdev` (personal portfolio); `mohangowdat-sail` is
  the future company-handoff destination, not the canonical home.** PipelineIQ
  is the user's personal pet project — `mohangowdatdev` is the primary GitHub
  account it should live under. Currently the 3 repos
  (`pipelineiq-architecture`, `pipelineiq-iac`, `pipelineiq-portal`) sit on
  `mohangowdat-sail`; that's a temporary state. Migration plan: transfer all 3
  repos to `mohangowdatdev` (both identities already authenticated locally),
  update local `git remote set-url`. Push to `mohangowdat-sail` only happens
  later as a one-time handoff with company-side customisations layered in. Not
  a DECISIONS-log item per user instruction — just a carry-over. Trigger is
  user's call, not "wait for architecture stable" — the user can flip it
  anytime.

---

## Where to read for specific situations

| Situation | Read this |
|---|---|
| Writing any notebook or SQL | SCHEMA.md |
| Starting a new phase | PROGRESS.md |
| Picking up after a session gap | PROGRESS.md → ## Session Log (last entry) |
| Unsure about a design choice | DECISIONS.md |
| Understanding full architecture | PLANNING.md |
| Writing a specific service's code | docs/{service}.md if it exists |
| ADF pipeline structure | docs/pipeline.md |
| pgvector / AI RCA logic | docs/ai_rca.md |
| Fixing a PostgreSQL schema issue | scripts/bootstrap_postgres.sql |
| Fixing an Azure SQL schema issue | scripts/bootstrap_sql.sql |
| Hit an error that feels familiar | docs/incident_log.md — check Index table before chasing speculative causes |
