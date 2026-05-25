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
Function App (FC1 Flex, daily 00:30 UTC, Logic-App-driven)
  └── writes orders + order_lines + status_log + dimension changes
        ↓
Databricks Job (daily 00:35 UTC, source_sim/write_inventory_snapshot)
  └── writes 189K-row inventory_snapshot (Spark JDBC bulk insert)
        ↓
Azure SQL Database (serverless, velora_oms)
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

## Architecture vs reality (read this before claiming "metadata-driven")

The diagram above is the **architected** topology, not the **built** one. Honest gap inventory as of S13:

| Architected | Built? | Notes |
|---|---|---|
| Azure SQL serverless source (`velora_oms`) | ✅ | Phase 1 + S6 real-dated reset, 17 days through 2026-05-13 |
| Generator on Function App (Flex) → source DB | ✅ | Writes orders/lines/status_log/dim changes (its sweet spot). Inventory write **REMOVED** in S14 (DECISIONS #71) — see next row. |
| **Databricks Job → inventory_snapshot** (S14, DECISIONS #71) | ✅ | Daily 00:35 UTC scheduled Job runs `notebooks/source_sim/write_inventory_snapshot.py`. Spark JDBC bulk insert, ~3-4 min wall. Replaces the Function App inventory write — S13's 3-pronged mitigation failed (11 consecutive partial fires 5/14-5/24). |
| Logic App schedule | ✅ | Fires Function at 00:30 UTC; Databricks Workflow has its own cron trigger at 00:35 UTC. |
| **ADF Copy Activity** landing extract | ❌ | **Tier 6 deferred.** Replaced today by `scripts/export_velora_to_landing.py` (laptop scaffold, hardcoded entity list). |
| ADLS Gen2 medallion (landing/bronze/silver/gold) | ✅ | Phase 2 fully complete |
| Bronze/Silver/Gold Databricks notebooks | ✅ | Entity-agnostic ingestion, all 12 entities through gold |
| Databricks SQL Warehouse | ✅ | Used for verification + future Power BI / VS Code |
| **PostgreSQL `pipeline.*` schema** (control plane) | ⚠️ **Built, NOT consumed** | Tables exist, `entity_registry` (12 rows) + `watermarks` (12 rows) seeded — but no consumer reads from them. ADF doesn't exist; bronze/silver/gold notebooks hardcode their entity. `process_queue` / `file_registry` / `pipeline_exec_log` are all 0 rows because nothing writes to them. |
| **PostgreSQL `pipelineiq.*` schema** (AI RCA) | ⚠️ Tables exist, 0 rows | `incident_store` populates in Phase 3 (failure injection + AI RCA). `iac_embeddings` populates in Phase 4 (IaC repo webhook + chunker). Both deferred. |
| Function REST API (`get_watermark`, `commit_watermark`, `register_file`) | ❌ | Architected but never written. The current Function only writes synthetic source data; it does not expose REST endpoints to ADF or notebooks. |
| FastAPI backend | ❌ Phase 5 | |
| React dashboard | ❌ Phase 6 | (Portal SPA exists separately at `pipelineiq-portal`.) |
| IaC repo push → embed → pgvector | ❌ Phase 4 | |

**The honest read:** the medallion is solid, the Postgres control plane is provisioned + seeded but **architecturally orphaned** (no consumer wires it in). The next architectural unit (Tier 6 + `pipeline.*` activation) closes this gap by making ADF the actual consumer of `entity_registry`, `watermarks`, `pipeline_exec_log`, and `file_registry` — that's when "metadata-driven" stops being a slide and becomes a fact.

**Why the gap exists:** Phase 2 (medallion) was prioritised as the demonstrable artifact. Tier 6 ADF + control-plane wiring was deferred so Phase 2 could ship via the laptop scaffold. The shortcut paid off (we have a complete medallion across 17 days of data); the cost is that "metadata-driven" remained a design promise, not a built feature, until Tier 6 lands.

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
| generator/ | **Inventory write removed (S14, DECISIONS #71); orders/lines/status_log only.** | DECISIONS #51: `yesterday_utc()` + idempotency guard. DECISIONS #61 (S11): `_connect_with_resume_retry` retries Azure SQL serverless wake-up errors. **DECISIONS #71 (S14): `run()` no longer calls `_write_inventory_snapshot` — inventory moved to Databricks Job (`notebooks/source_sim/`). The function definition stays for `scripts/inventory_only.py` recovery path. Supersedes #62 + #69 (chunked inventory + worker-kill mitigation — both failed in the wild; 11 consecutive partial fires 5/14-5/24).** DECISIONS #70 (S13): `aad` mode in `config.py` so `scripts/inventory_only.py` works on laptop without password. Source DB has **24 real-dated days Apr 27 → May 24, 2026** (S14 recovered 5/14-5/24 inventory via laptop). Manual backfills must stop at `today-1`; re-seed of an already-populated date requires explicit wipe. `AZURE_SQL_AUTH_MODE=aad ... python scripts/inventory_only.py --date YYYY-MM-DD` is the laptop fallback for inventory-only writes when the autonomous Databricks Job missed a date. |
| notebooks/source_sim/ | **Live (S14)** | `write_inventory_snapshot.py` — Spark notebook, deterministic 189,225-row synthesis per `snapshot_date` via xxhash64 on (product_id, store_id, date, salt). Reads `velora_pim.products`/`stores` via JDBC, writes `velora_pim.inventory_snapshot` via JDBC mode=append + numPartitions=8 + batchsize=10000. `verify_order_landed` widget refuses to write if `velora_oms.orders` has 0 rows for the date (two-writer race protection). `force=true` wipes + rewrites for idempotency. Scheduled via `core/inventory_workflow/` IaC at 00:35 UTC daily. DECISIONS #71. |
| notebooks/bronze/ | **All 12 entities hydrated (S7 + S9.5)** | `ingest_to_bronze.py` is entity-agnostic (DECISIONS #48). 10 main entities (~1.9M rows) + 2 static seeds added in S9.5: `bronze.default.product_categories` (35 rows) + `bronze.default.stores` (45 rows). DECISIONS #60. |
| notebooks/silver/ | **All 10 tables done (S8 + S9.5 + S11.2).** | All 10 silvers live with 100% DQ pass on real-dated source through 2026-05-10. Counts: `orders` (~4.5K), `customers` (~340), `order_lines` (~18K), `products` (4,205), `product_pricing` (4,218), `sales_reps` (30), `territory_assignments` (30), `inventory_snapshot` (2,648,025 — partitioned by `snapshot_date` per DECISIONS #63), `order_status_log` (11,694, allows `RETURN_INITIATED` per DECISIONS #64), `customer_addresses` (339, every customer has 1 primary). |
| notebooks/gold/ | **All 12 dims+facts done (S8 + S9.5 + S10 + S11.2 + S12 catch-up + S13 SCD-2 fix).** | 9 dims + 3 facts live. **S12:** `dim_order_status` extended 6 → 7 rows with `RETURN_INITIATED`. **S13 (DECISIONS #68):** `build_gold_dim_customer.py` patched — `df_actioned.cache()` after categorization eliminates the lazy-eval bug that was producing duplicate surrogate_keys on SCD2_CHANGE rows. `dim_product` + `dim_sales_rep` are CLEAN (verified) — they don't have the bug because they derive valid_from from immutable source-effective dates. Historical 51 collisions in `dim_customer` cleaned up via one-off SQL MERGE (set current row's valid_from = closed.valid_to + 1, recompute sk). Result: 407 → 407 distinct SKs → 0 collisions. Phase 2 medallion fully complete. |
| functions/ | **Stable + simplified in S14 — inventory write removed (DECISIONS #71). Function now owns orders/lines/status_log/dim-changes only.** | Migrated Y1 → FC1 Flex Consumption (DECISIONS #50). **S9 (DECISIONS #59):** Daily fire moved to a Logic App (`pipelineiq-scheduler-dev`, `core/scheduler/` IaC) that POSTs to `/admin/functions/generator` at 00:30 UTC. Function `host.json` `functionTimeout` bumped 10m → 30m (overkill now that inventory is out — could drop to 5m). **S11 (DECISIONS #61):** `_connect_with_resume_retry` retries on 40613/HYT00 — still active and needed for cold-start. **S12 (DECISIONS #66):** `azure-monitor-opentelemetry` SDK + LA `AppTraces` telemetry — still active. **S13 (DECISIONS #69): timer trigger disabled** (`function.json` schedule = Feb 31 = never; Logic App admin-invoke is sole fire path) — still active and correct regardless of the inventory migration. **S14 (DECISIONS #71): inventory write removed from `run()`** — the structural Flex worker-kill problem that #62 + #69 tried to solve is now solved by moving the workload to Databricks (`notebooks/source_sim/`) where there's no Flex reaper and Spark JDBC bulk insert is 30-50× faster than pyodbc. Function fire now expected in ~2 min wall (was 30+ min theoretical). |
| inventory_workflow/ (IaC `core/inventory_workflow/`) | **Live (S14)** | Databricks Job module: scheduled trigger `0 35 0 ? * *` (00:35 UTC daily, 5 min after Function), single-node Standard_DS3_v2 DBR 14.3 cluster, references workspace notebook `/Shared/pipelineiq/source_sim/write_inventory_snapshot`. Cluster auto-terminates per the jobs cluster policy. Notebook deploy is out-of-band via `scripts/run_inventory_smoke.py` (same pattern as bronze/silver/gold). DECISIONS #71. |
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
- ~~App Insights telemetry not flowing on Flex Consumption~~ **Resolved (S12, DECISIONS #66).**
  The original diagnosis was wrong — AI is workspace-backed, so data was in LA tables
  `AppTraces` / `AppRequests` / `AppExceptions` all along, not the classic AI
  tables that `az monitor app-insights query` hits. S12 also added
  `azure-monitor-opentelemetry` SDK init in `generator/main.py` for clean
  structured logging + Function App diagnostic settings → `pipelineiq-logs-dev`
  for platform-side `FunctionAppLogs`. **Query path:** use Log Analytics on
  `pipelineiq-logs-dev` workspace (KQL on `AppTraces` etc), not classic AI CLI.
- ~~Verify the 2026-05-13 00:30 UTC autonomous fire~~ **Done (S13).** Fire wrote
  orders/lines/status cleanly (40613 retry worked) but inventory died at 20K/189K
  AGAIN — making it 3-for-3 deterministic Flex worker-kill (5/11, 5/12, 5/14
  fires all died at exactly 20K rows ~85-130s into inventory). Recovered via
  `scripts/inventory_only.py` with the new `aad` mode. **S13 applied 3-pronged
  structural mitigation** (DECISIONS #69): (1) timer trigger disabled (`function.json`
  schedule = Feb 31 = never fires) — Logic App admin-invoke is sole path,
  eliminates the timer/Logic-App race; (2) inventory chunk_size 10K → 5K (more
  frequent commits); (3) per-sub-batch `logger.info` in `_write_inventory_snapshot`
  (gRPC traffic every ~2.5s instead of ~25s). Function deployed.
- **Verify the 2026-05-15 00:30 UTC autonomous fire** (writes 2026-05-14) —
  the canonical proof under all three S13 mitigations. Expected: single fire
  (no timer race), 38 chunk-commit traces (5K each, 189225 / 5000 ≈ 38), ~190
  sub-batch traces. If inventory still partial-dies at any N×5K boundary, the
  3-pronged mitigation didn't fix the root cause — escalate to (a) move
  inventory writing OUT of the Function to a Databricks scheduled job, OR
  (b) upgrade Function App to EP1 Premium (always-on instance, no idle reaper).
  Verification SQL + KQL queued in PROGRESS.md `## Next task`.
- **Tier 6 ADF (Bicep) not yet written.** Linked services (SQL, ADLS, KV, Databricks)
  + parameterised datasets + copy pipeline `velora_oms.*` → `landing/`. Replaces
  `scripts/export_velora_to_landing.py` in production. Not blocking Phase 2 dev —
  the scaffold script is a reasonable substitute until Tier 6 lands.
- ~~`docs/runbooks/databricks_account_admin_bootstrap.md` step 5~~ **Fixed (S12).**
- ~~`scripts/inventory_only.py` needs AAD auth mode~~ **Done (S13, DECISIONS #70).**
  `generator/config.py` now exposes `aad` mode + `connect_aad()` helper that uses
  `DefaultAzureCredential` + ODBC token attr (`SQL_COPT_SS_ACCESS_TOKEN=1256`).
  Usage: `AZURE_SQL_AUTH_MODE=aad ... python scripts/inventory_only.py --date 2026-05-12`.
- ~~SCD-2 `valid_from` bug for CHANGED rows in `dim_customer`~~ **Done (S13, DECISIONS #68 — supersedes #67).**
  Real bug was Spark lazy-eval, NOT a `valid_from` formula bug as DECISIONS #67
  claimed. `df_actioned` was being recomputed in step 6 AFTER step 4 had flipped
  is_current=false, causing SCD2_CHANGE rows to re-categorize as NEW with
  valid_from=first_activity_date (collision). Fix was a single `df_actioned.cache()`
  in `build_gold_dim_customer.py`. `dim_product` + `dim_sales_rep` are CLEAN
  (verified — they use source-effective dates, not joins against the dim's own
  is_current). Historical 51 collisions cleaned in-place via SQL MERGE (set
  current row's `valid_from = closed.valid_to + 1`, recompute sk). 407 → 407
  distinct SKs → 0 collisions. fact_order_line was rebuilt during the gold
  catch-up wave so its customer_surrogate_key picks up the corrected sks.
- **Cosmetic: each user log appears twice in `AppTraces`** (OT handler
  attached by `configure_azure_monitor` + the Functions runtime's root
  handler both forward). Doesn't affect debuggability. Fix:
  `logging.getLogger("generator").propagate = False` right after
  `configure_azure_monitor()` in `generator/main.py`. Defer to the next
  generator deploy round-trip — not worth a deploy on its own.
- **Portal Preview + Development env vars** (2026-04-21 S2). Only Production has
  `AZURE_OPENAI_API_KEY` in Vercel — CLI blocked Preview on branch-scoping and
  Development on the `--sensitive` flag. Dashboard overrides both. Not blocking
  the live site; finish via dashboard if/when PR previews or `vercel dev` are
  needed.
- **Repos live on `mohangowdatdev` (personal portfolio); `mohangowdat-sail` is
  the future one-time company-handoff destination.** PipelineIQ is the user's
  personal pet project. All 3 repos (`pipelineiq-architecture`,
  `pipelineiq-iac`, `pipelineiq-portal`) are already on `mohangowdatdev` as of
  S11.2 — verified via `git remote -v` in S12. Future step: a one-time push to
  `mohangowdat-sail` with company-side customisations layered in. Trigger is
  user's call — not "wait for architecture stable". Not a DECISIONS-log item
  per user instruction — just a carry-over so future sessions don't
  re-litigate the org choice.

---

## Where to read for specific situations

| Situation | Read this |
|---|---|
| Writing any notebook or SQL | SCHEMA.md |
| Starting a new phase | PROGRESS.md + docs/forward_plan.md |
| Picking up after a session gap | PROGRESS.md → ## Session Log (last entry) |
| Sequencing the work that remains | docs/forward_plan.md (S15 → S21 outline + dependency graph) |
| Phase exit criteria | PLANNING.md → ## Phase-by-phase exit criteria |
| Resource-level "what exists in Azure" | docs/build_order.md |
| Unsure about a design choice | DECISIONS.md |
| Understanding full architecture | PLANNING.md |
| Writing a specific service's code | docs/{service}.md if it exists |
| ADF pipeline structure | docs/pipeline.md |
| pgvector / AI RCA logic | docs/ai_rca.md |
| Fixing a PostgreSQL schema issue | scripts/bootstrap_postgres.sql |
| Fixing an Azure SQL schema issue | scripts/bootstrap_sql.sql |
| Hit an error that feels familiar | docs/incident_log.md — check Index table before chasing speculative causes |
