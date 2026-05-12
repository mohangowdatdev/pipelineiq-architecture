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
| generator/ | **Real-dated + guarded (S6) + 40613-resilient (S11)** | DECISIONS #51: `yesterday_utc()` + idempotency guard. DECISIONS #61 (S11): `_connect_with_resume_retry` retries Azure SQL serverless wake-up errors. DECISIONS #62 (S11): chunked inventory write w/ per-chunk commits. Source DB has 15 real-dated days Apr 27 → May 11, 2026. Manual backfills must stop at `today-1`; re-seed of an already-populated date requires explicit wipe. `scripts/inventory_only.py --date YYYY-MM-DD` is the laptop fallback for inventory-only writes when the function fire dropped inventory. |
| notebooks/bronze/ | **All 12 entities hydrated (S7 + S9.5)** | `ingest_to_bronze.py` is entity-agnostic (DECISIONS #48). 10 main entities (~1.9M rows) + 2 static seeds added in S9.5: `bronze.default.product_categories` (35 rows) + `bronze.default.stores` (45 rows). DECISIONS #60. |
| notebooks/silver/ | **All 10 tables done (S8 + S9.5 + S11.2).** | All 10 silvers live with 100% DQ pass on real-dated source through 2026-05-10. Counts: `orders` (~4.5K), `customers` (~340), `order_lines` (~18K), `products` (4,205), `product_pricing` (4,218), `sales_reps` (30), `territory_assignments` (30), `inventory_snapshot` (2,648,025 — partitioned by `snapshot_date` per DECISIONS #63), `order_status_log` (11,694, allows `RETURN_INITIATED` per DECISIONS #64), `customer_addresses` (339, every customer has 1 primary). |
| notebooks/gold/ | **All 12 dims+facts done (S8 + S9.5 + S10 + S11.2 + S12 catch-up).** | 9 dims + 3 facts live. **S12 update:** `dim_order_status` extended from 6 → 7 rows with `RETURN_INITIATED` (closes DECISIONS #64 gap). 5/11 caught up end-to-end: `fact_order_line` 19,352, `fact_inventory_daily` 2,837,250 (silver↔gold reconcile exactly), `dim_customer` 407 (SCD-2 grew from 339), `dim_product` 4,223 (SCD-2 grew with 5 new price-change rows). Phase 2 medallion **fully complete**. |
| functions/ | **Stable on FC1 Flex + Logic-App schedule + S11 cold-start/inventory fixes + S12 telemetry** | Migrated Y1 → FC1 Flex Consumption (DECISIONS #50). **S9 (DECISIONS #59):** Daily fire moved to a Logic App (`pipelineiq-scheduler-dev`, `core/scheduler/` IaC) that POSTs to `/admin/functions/generator` at 00:30 UTC. Function `host.json` `functionTimeout` bumped 10m → 30m. **S11 (DECISIONS #61 + #62):** `_connect_with_resume_retry` retries on 40613/HYT00; inventory write chunked at 10K rows/chunk with per-chunk commit + progress logging. **S12 (DECISIONS #66):** `azure-monitor-opentelemetry` SDK init in `generator/main.py` so user-code `logger.info` calls reach LA `AppTraces` (workspace-backed AI, not classic). Function App diagnostic settings → `pipelineiq-logs-dev` for platform-side `FunctionAppLogs`. Function timer remains registered as harmless fallback (idempotency guard from #51 makes double-fire safe). ADF replacements for Bronze chain still pending in Tier 6. **2026-05-12 fire partial:** orders/lines/status_log clean, inventory died at 20K/189K — recovered via laptop AAD-token script. Next fire (2026-05-13) is the canonical proof under all 3 fixes. |
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
- **Verify the 2026-05-13 00:30 UTC autonomous fire** (writes 2026-05-12) —
  the first fire under both the S11 fixes (#61 + #62) **and** the S12 telemetry
  fix (#66). Previous fire (2026-05-12, writing 5/11) cold-start retry worked
  (orders + lines + status_log committed cleanly) but inventory partial-died
  at exactly 20,000 / 189,225 rows — same host-worker-kill bug as 5/11 manual
  fire. With #66 in place, `AppTraces` should now show every 10K chunk-commit
  trace, pinpointing the death point if it fires again. Check via:
  ```sql
  -- on velora_oms
  SELECT order_date, COUNT(*) FROM velora_oms.orders WHERE order_date='2026-05-12' GROUP BY order_date;
  SELECT COUNT(*) FROM velora_pim.inventory_snapshot WHERE snapshot_date='2026-05-12';
  SELECT COUNT(*) FROM velora_oms.order_status_log WHERE created_at >= '2026-05-13T00:25:00';
  ```
  And in LA workspace (NOT classic AI):
  ```kusto
  AppTraces
  | where TimeGenerated between (datetime(2026-05-13T00:25:00Z)..datetime(2026-05-13T01:00:00Z))
  | where Message has 'Inventory' or Message has 'committed' or Message has 'generator'
  | project TimeGenerated, SeverityLevel, Message
  | order by TimeGenerated asc
  ```
  If inventory comes up partial again, look for the last `Inventory snapshot:
  committed N / 189225 rows` trace — that's the diagnostic gain. Then
  investigate gRPC host-worker timeout on Flex or scale to EP1 Premium.
  Laptop fallback: ad-hoc AAD-token recovery script (S12 inlined to `/tmp` —
  worth promoting to a real `scripts/recover_inventory.py` with AAD auth if
  this fires again).
- **Tier 6 ADF (Bicep) not yet written.** Linked services (SQL, ADLS, KV, Databricks)
  + parameterised datasets + copy pipeline `velora_oms.*` → `landing/`. Replaces
  `scripts/export_velora_to_landing.py` in production. Not blocking Phase 2 dev —
  the scaffold script is a reasonable substitute until Tier 6 lands.
- ~~`docs/runbooks/databricks_account_admin_bootstrap.md` step 5~~ **Fixed (S12).**
- **`scripts/inventory_only.py` needs AAD auth mode in `generator/config.py`.**
  Currently the script imports `config.get_connection_string()` which only
  supports `password` (laptop) or `msi` (function). On laptop without
  `AZURE_SQL_PASSWORD` it fails. S12 sidestepped via inline AAD-token script.
  Cleanup: add `aad` mode to `config.py` that uses `DefaultAzureCredential`
  + ODBC token attr (`SQL_COPT_SS_ACCESS_TOKEN=1256`). Low priority.
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
