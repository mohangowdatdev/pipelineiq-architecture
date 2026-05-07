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
| generator/ | **Real-dated + guarded (S6)** | DECISIONS #51: `yesterday_utc()` + idempotency guard. Source DB has 10 real-dated days Apr 27 → May 6, 2026. Manual backfills must stop at `today-1`; re-seed of an already-populated date requires explicit wipe. |
| notebooks/bronze/ | **All 10 entities hydrated (S7)** | `ingest_to_bronze.py` is entity-agnostic (DECISIONS #48). All 10 `bronze.default.*` tables hydrated (~1.9M rows). |
| notebooks/silver/ | **Pattern-setters live (S8). 2/10 tables done.** | `silver.orders` (3,619 rows) + `silver.customers` (248 rows). Conventions documented in SCHEMA.md. SCD-change tracking computed at Silver (per DECISIONS #52). Remaining 8 tables follow the same shape. |
| notebooks/gold/ | **First SCD-2 dim live (S8). 1/12 dims+facts done.** | `gold.dim_customer` (248 rows, idempotent). Surrogate key + valid_from + as-of-join conventions in SCHEMA.md (DECISIONS #52, #53, #56). Remaining 11 follow the same shape. |
| functions/ | **Stable on FC1 Flex (S6)** | Migrated Y1 → FC1 Flex Consumption (DECISIONS #50): Y1 + Linux + non-HTTP triggers was the documented sad path — silent timer drops. Cron `0 0 6 * * *` unchanged. Real-world reliability proof = next 06:00 UTC fire. ADF replacements for Bronze chain still pending in Tier 6. |
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
- **App Insights telemetry not flowing on Flex Consumption.** The Function
  executes (proven via `velora_oms` side-effects) but `requests` / `traces` /
  `exceptions` tables in App Insights stay empty. Was also broken on Y1.
  Likely a Flex-specific instrumentation tweak (Python OpenTelemetry mode,
  worker extension, or auto-instrumentation app setting). Investigate when
  observability is needed for Phase 4+ — not blocking Phase 2.
- **Verify tomorrow's first 06:00-IST autonomous fire** (2026-05-08 00:30 UTC).
  S7 changed the cron from 06:00 UTC → 06:00 IST (00:30 UTC); today's data
  was a manual fire because the change happened mid-day. Tomorrow's fire is
  the actual reliability proof. Check via:
  ```sql
  SELECT order_date, COUNT(*) AS orders_count, MIN(created_at) AS first_insert_utc
  FROM velora_oms.orders GROUP BY order_date ORDER BY order_date;
  ```
  Expect a 2026-05-07 row with `first_insert_utc` between 00:30–00:40 UTC and
  270–500 orders. After verifying, **re-run the silver/gold layer**
  (`run_silver_smoke.py --entity orders|customers` + `run_gold_smoke.py
  --entity dim_customer`) to incorporate May-7 data.
- **Rotate `AzureWebJobsStorage` account key on `pipelineiqfunctionsdev`.**
  Key was printed in S6 transcript when listing app settings (~`vz1Z2iSghWL6...`).
  Storage Account → Keys and Endpoint → Regenerate key1; Terraform will pick
  up the new key on next apply. Same exposure class as the Portal Key 1
  rotation item below.
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
- **Rotate Key 1 on `pipeline-iq-resource`** (2026-04-21 S2). First/last 8 chars
  (`G2F8oS21...ACOGwlg6`, 16 of 84) appeared in S2 transcript during debug. Azure
  Portal → resource → Keys and Endpoint → Regenerate Key 1 → update the Vercel
  env var (`az cognitiveservices account keys list ...` piped to
  `vercel env add AZURE_OPENAI_API_KEY production --value "$K" --yes --sensitive`).
  Low exposure; recommended as hygiene.

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
