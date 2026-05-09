# PipelineIQ — Build Order

Canonical, dependency-ordered sequence for provisioning every PipelineIQ
resource. Each tier depends on the tier above it. Tiers within a group
can often be built in parallel, but never before their dependencies.

**This is the tracking document.** Update the status column as each item
lands. A session that builds infrastructure should leave this file in a
state where the next session can see exactly what exists in Azure and what
does not.

---

## How to use this file

1. When you start work, find the lowest-tier row that is `Pending` or
   `Blocked` — that is the next thing to build.
2. When an item completes, update the status to `Done`, fill the date,
   and point to the Terraform path or command that produced it.
3. If an item is blocked, write `Blocked` and note the blocker.
4. Never skip a tier. If tier 3 needs something from tier 2, tier 2 is
   not optional.

Status values: `Pending` | `In progress` | `Done` | `Blocked` | `N/A`

---

## Tier 0 — Local developer environment

Machine-level setup needed before any cloud work. One-time per developer.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 0.1 | Homebrew + git | Done | 2026-04-21 | system | Pre-existing |
| 0.2 | Azure CLI (`az`) logged in | Done | 2026-04-21 | `az login` | Sail Analytics tenant |
| 0.3 | Python 3.11 | Done | 2026-04-21 | `brew install python@3.11` | Matches Azure Functions runtime |
| 0.4 | `.venv/` created | Done | 2026-04-21 | `python3.11 -m venv .venv` | Project virtualenv |
| 0.5 | Generator Python deps installed | Done | 2026-04-21 | `pip install -r generator/requirements.txt` | pandas, numpy, faker, pyodbc, sqlalchemy, azure-identity |
| 0.6 | direnv installed + hooked | Done | 2026-04-21 | `brew install direnv` + `~/.zshrc` hook | Auto-activates venv + loads .env on cd |
| 0.7 | `.envrc` + `.env` written, `direnv allow` run | Done | 2026-04-21 | `.envrc` committed, `.env` gitignored | Sponsorship sub + Sail tenant IDs in .env |
| 0.8 | Microsoft ODBC Driver 18 for SQL Server | Done | 2026-04-21 | `HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18` | `msodbcsql18` 18.6.2.1 + `mssql-tools18` 18.6.2.1 installed (S3). `HOMEBREW_ACCEPT_EULA=Y` was the right env var (not `ACCEPT_EULA=Y`). `sqlcmd` + `bcp` at `/opt/homebrew/bin/`. |
| 0.9 | Terraform CLI (>= 1.6) | Done | 2026-04-21 | `brew install hashicorp/tap/terraform` | v1.14.8. HashiCorp's own tap — `brew install terraform` on default registry is stale/absent |
| 0.10 | Microsoft ODBC Driver 18 install finished | Done | 2026-04-21 | — | Duplicate of 0.8 — resolved same session. |
| 0.11 | `PipelineIQ-IaC/` sibling repo scaffolded + git init | Done | 2026-04-21 | `mkdir` + `git init` | Folder structure per CLAUDE.md: `core/`, `source_connectors/*`, `clients/velora/`, `pipelineiq_app/`, `bicep/adf/`, `scripts/`. CLAUDE.md + README.md + .gitignore + .terraform-version written. Separate git repo, not pushed yet. |

---

## Tier 1 — Azure subscription & state backend

Must exist before any `terraform apply`. State lives outside Terraform itself.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 1.1 | Sponsorship subscription selected | Done | 2026-04-21 | `az account set --subscription ea05f17f-…` | Microsoft Azure Sponsorship, tenant 23d48723-… |
| 1.2 | Resource group `pipelineiq-rg-dev` | Done | 2026-04-21 | `scripts/bootstrap_state.sh` | Central India |
| 1.3 | Storage account `pipelineiqtfstate` | Done | 2026-04-21 | `scripts/bootstrap_state.sh` | Standard_LRS, TLS 1.2, no public blob access |
| 1.4 | Blob container `tfstate` | Done | 2026-04-21 | `scripts/bootstrap_state.sh` | Holds Terraform state files |

Blocker for next tier: all of 1.1–1.4 must be Done.

---

## Tier 2 — Core platform (Terraform)

The foundational Azure resources every other module depends on. Lives in
`pipelineiq-iac/core/`. Build order within this tier matters — Key Vault
before anything that needs a secret; Log Analytics before anything wired
to diagnostic settings.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 2.1 | Terraform `backend "azurerm"` configured | Done | 2026-04-21 | `PipelineIQ-IaC/clients/velora/backend.tf` | azurerm backend → `pipelineiq-rg-dev` / `pipelineiqtfstate` / `tfstate` / `velora.tfstate` |
| 2.2 | `terraform init` run in `clients/velora/` | Done | 2026-04-21 | `terraform init` | azurerm v4.69.0 installed; lock file written |
| 2.3a | Core submodules written (`keyvault/`, `log_analytics/`, `adls/`) | Done | 2026-04-21 | `PipelineIQ-IaC/core/{keyvault,log_analytics,adls}/` | Submodule pattern per DECISIONS.md #29. Each: main.tf + variables.tf + outputs.tf |
| 2.3b | Velora composition wired | Done | 2026-04-21 | `PipelineIQ-IaC/clients/velora/main.tf` | Calls all 3 core modules with common tags and name prefix/suffix |
| 2.3c | `terraform plan` clean | Done | 2026-04-21 | `tfplan` saved | 10 resources to add: KV + 2 role assignments + LAW + Storage + 5 ADLS filesystems |
| 2.3 | Key Vault `pipelineiq-kv-dev` applied | Done | 2026-04-21 | `terraform apply tfplan` (S3) | RBAC-auth, purge protection off, soft-delete 7d. Required Owner role on RG — user elevated mid-session (DECISIONS #35). |
| 2.4 | Log Analytics workspace `pipelineiq-logs-dev` applied | Done | 2026-04-21 | `terraform apply tfplan` (S3) | 30-day retention, PerGB2018, no daily quota |
| 2.5 | ADLS Gen2 `pipelineiqadlsdev` + filesystems applied | Done | 2026-04-21 | `terraform apply tfplan` (S3) | HNS enabled; 5 filesystems all live: landing/bronze/silver/gold/quarantine |
| 2.6 | Managed identities for ADF + Databricks + Functions | Deferred | — | — | Deferred per DECISIONS.md #31 — system-assigned identities created alongside each downstream resource instead |

Blocker for next tier: none — all Done.

---

## Tier 3 — Source connector (Velora / Azure SQL)

Client-specific source system provisioning. Lives in
`pipelineiq-iac/source_connectors/azure_sql/` module, invoked from
`pipelineiq-iac/clients/velora/`.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 3.1 | Azure SQL logical server `pipelineiq-sql-velora-dev` | Done | 2026-04-21 | `PipelineIQ-IaC/source_connectors/azure_sql/main.tf` | v12.0, TLS 1.2 min, AAD admin = current user + dual password auth (DECISIONS #36) |
| 3.2 | Azure SQL Database `velora_oms` (serverless, 2 vCore max) | Done | 2026-04-21 | `PipelineIQ-IaC/source_connectors/azure_sql/main.tf` | `GP_S_Gen5_2`, min 0.5 vCore, auto-pause 60 min, 32 GB max |
| 3.3a | Firewall rule: allow Azure services | Done | 2026-04-21 | Terraform `allow_azure_services` rule | 0.0.0.0–0.0.0.0 (Azure-internal) |
| 3.3b | Firewall rule: local dev IP | Done | 2026-04-21 | `current_ip` var in `clients/velora/terraform.tfvars` | `69.5.168.130` added to both PG + SQL via tf apply |
| 3.4 | SQL admin password + connection string in Key Vault | Done | 2026-04-21 | `clients/velora/main.tf` → `azurerm_key_vault_secret` `sql-admin-password` + `sql-connection-string` | Generated via `random_password`, 24 chars |
| 3.5 | `bootstrap_sql.sql` executed against `velora_oms` | Done | 2026-04-21 | `.venv/bin/python scripts/run_bootstrap_sql.py` | 18 T-SQL batches all OK. Ran via pyodbc + AAD token (old ODBC sqlcmd doesn't support `--authentication-method`). All 4 schemas + 11 tables + control_flags created. |

Blocker for Tier 5: none.

---

## Tier 4 — Control plane (PostgreSQL + Functions)

PipelineIQ's own state: watermarks, file registry, incident store,
pgvector IaC embeddings. Lives in `pipelineiq-iac/core/postgres.tf` and
`pipelineiq-iac/core/functions.tf`.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 4.1 | PostgreSQL Flexible Server `pipelineiq-pg-dev` (B2s) | Done | 2026-04-21 | `PipelineIQ-IaC/core/postgres/main.tf` | B_Standard_B2s, PG 16, 32 GB, zone 1, dual auth (password + AAD) |
| 4.2 | `pgvector` extension enabled (server allowlist) | Done | 2026-04-21 | `allowed_extensions = ["vector", "pg_trgm", "uuid-ossp"]` | Values MUST be lowercase (DECISIONS #37). `CREATE EXTENSION` still needed in bootstrap_postgres.sql |
| 4.3a | Firewall rule: allow Azure services | Done | 2026-04-21 | Terraform `allow_azure_services` rule | |
| 4.3b | Firewall rule: local dev IP | Done | 2026-04-21 | `current_ip` var | `69.5.168.130` |
| 4.4 | Postgres admin password + connection string in Key Vault | Done | 2026-04-21 | `azurerm_key_vault_secret` `postgres-admin-password` + `postgres-connection-string` | Generated via `random_password`, 24 chars; AAD admin = current user is the preferred path |
| 4.5 | `bootstrap_postgres.sql` executed against `postgres` DB | Done | 2026-04-21 | `PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv) psql ...` | `pipeline` + `pipelineiq` schemas, 6 control tables, pgvector extension, `iac_embeddings` ivfflat index, 10 entity_registry rows seeded |
| 4.6 | Azure Functions app `pipelineiq-functions-dev` (Python 3.11, **Flex Consumption FC1**) | Done | 2026-05-06 | `PipelineIQ-IaC/core/functions/` | **Migrated Y1 → FC1 in S6** (DECISIONS #50): Y1 Linux Consumption silently dropped scheduled timer fires. Now FC1 + private deployment-package container + system-assigned MSI. MSI re-granted on velora_oms after destroy/recreate (new principal `ccdac37d-...`). Generator now uses `today_utc - 1` (DECISIONS #51 supersedes #49 date-resolution) + idempotency guard skips already-seeded dates. **S9: `functionTimeout` bumped 10m → 30m** in `host.json` — partial executions on Flex were timing out during the 189K-row inventory write. Cron unchanged: `0 30 0 * * *` (00:30 UTC = 06:00 IST), but **timer trigger no longer the source of truth** — see 4.7. |
| 4.7 | Logic App `pipelineiq-scheduler-dev` (Consumption) — daily 00:30 UTC POST → function admin endpoint | Done | 2026-05-09 | `PipelineIQ-IaC/core/scheduler/` | **DECISIONS #59.** Created in S9 because Flex Consumption timer triggers don't reliably fire from cold (verified 2026-05-09: 2 consecutive scheduled fires for May 7 + May 8 silently no-op'd despite a healthy function + correct cron — App Insights showed 0 host-startup events for those windows). Logic App is the new source of truth for the daily fire; function timer remains registered as happy-bonus fallback (idempotency guard from DECISIONS #51 makes any double-fire safe). Plumbing verified end-to-end via REST trigger 08:46 UTC: Logic App→function host wake→generator `run()`→guard short-circuit. Cost: ~Rs.0/mo (1 fire/day << 4,000-action free grant). |

Blocker for Tier 7: 4.5–4.7 Done.

---

## Tier 5 — Compute & data lakehouse (Databricks)

Databricks workspace, Unity Catalog metastore, clusters, SQL Warehouse.
Depends on Tier 2 (ADLS, Key Vault, Log Analytics) and Tier 4 (for job
secrets referenced via Key Vault).

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 5.1 | Databricks workspace `pipelineiq-dbx-dev` (Premium) | Done | 2026-04-21 | `PipelineIQ-IaC/core/databricks/main.tf` | Premium SKU, managed RG `pipelineiq-dbx-dev-managed-rg`. Workspace module deliberately minimal — UC setup deferred to 5.2. |
| 5.2 | Unity Catalog metastore attached | Done | 2026-05-01 | `core/databricks_uc/main.tf` (data source) | **Adopted system metastore** `metastore_azure_centralindia` (id `a2d5ffb1-1ac9-42ec-babb-80eacf4ba2fb`) — Databricks limits 1 metastore per region per account, and one was auto-created when `mohan.gowda` first opened the account console. Workspace was already auto-assigned. DECISIONS #46. |
| 5.3 | External location: ADLS containers `landing`, `bronze`, `silver`, `gold`, `quarantine` | Done | 2026-05-01 | `core/databricks_uc/main.tf` `databricks_external_location.this` | All 5 created. Backed by `pipelineiq-dev-sc` storage credential (managed identity from access connector). |
| 5.4 | Unity Catalog catalogs: `bronze`, `silver`, `gold`, `quarantine` | Done | 2026-05-07 | `core/databricks_uc/main.tf` `databricks_catalog.this` | Storage_root per catalog — each rooted at `abfss://{name}@pipelineiqadlsdev.dfs.core.windows.net/`. `default` schema auto-created on first table write. `quarantine` added in S8 (was previously listed as external_location only — `var.catalogs` default updated from 3 to 4 entries). |
| 5.5 | Jobs Compute cluster policy | Done | 2026-05-01 | `core/databricks_uc/main.tf` `databricks_cluster_policy.jobs` | Policy ID `000E52A43E9F9628`. DS3_v2 fixed, max 2 workers range. Note: policy enforces autotermination — only valid for all-purpose clusters. Job clusters auto-terminate when job ends, so smoke-test runs don't reference the policy_id. |
| 5.6 | SQL Warehouse `pipelineiq-dev-sqlwh` (2X-Small, auto-stop 10m) | Done | 2026-05-01 | `core/databricks_uc/main.tf` `databricks_sql_endpoint.this` | ID `71a1e581f197abf0`. Classic tier. Verified by `bronze.default.customers` SELECT statement returning 158 rows. |
| 5.7 | Secret scope backed by Key Vault | Done | 2026-05-01 | `core/databricks_uc/main.tf` `databricks_secret_scope.kv` | Name `pipelineiq-dev-kv`. AAD-backed pointer to Key Vault `pipelineiq-kv-dev`. |
| 5.8 | Databricks access connector (managed identity for ADLS) | Done | 2026-05-01 | `core/databricks_uc/main.tf` `azurerm_databricks_access_connector.this` | `pipelineiq-dev-dbx-ac`. System-assigned identity. Granted Storage Blob Data Contributor on `pipelineiqadlsdev` via `azurerm_role_assignment.ac_blob_contributor`. |

Blocker for Tier 6: 5.1–5.7 Done.

---

## Tier 6 — Orchestration (ADF)

Parameterised ADF pipeline per source connector. Bicep rather than
Terraform for ADF-internal objects (linked services, datasets, pipelines).

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 6.1 | Data Factory `pipelineiq-adf-dev` | Pending | — | `pipelineiq-iac/core/adf.tf` | Managed identity, Git disabled (bicep-first) |
| 6.2 | Linked services: Azure SQL, ADLS, Key Vault, Databricks | Pending | — | `pipelineiq-iac/pipelineiq_app/adf/*.bicep` | |
| 6.3 | Parameterised datasets (Velora) | Pending | — | Bicep | One per source table |
| 6.4 | Copy pipeline: Azure SQL → landing | Pending | — | Bicep | Watermark-based incremental |
| 6.5 | Databricks notebook activities: Bronze / Silver / Gold | Pending | — | Bicep | Each reads `_pipeline_run_id` param |
| 6.6 | Diagnostic settings → Log Analytics | Pending | — | Terraform | All pipeline run events streamed |

Blocker for Tier 8: 6.1–6.6 Done.

---

## Tier 7 — AI & RCA

Azure OpenAI + FastAPI backend. Note: Azure OpenAI lives in
**South India** (not Central India) — centralindia does not yet host
the Azure OpenAI service. Cross-region call is async / batched, latency
immaterial. See DECISIONS.md #25.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 7.1 | Azure OpenAI account `pipelineiq-openai-dev` (South India) | Done | 2026-04-21 | `PipelineIQ-IaC/core/openai/main.tf` | S0 SKU, custom_subdomain = account name → `https://pipelineiq-openai-dev.openai.azure.com/` |
| 7.2 | GPT-4o model deployment | Done | 2026-04-21 | `azurerm_cognitive_deployment.this["gpt-4o"]` | Model version `2024-11-20`, Standard SKU, capacity 10. South India hosts GPT-4o — confirmed. |
| 7.3 | OpenAI key + endpoint stored in Key Vault | Done | 2026-04-21 | `azurerm_key_vault_secret` `openai-api-key` + `openai-endpoint` | |
| 7.4 | Container Apps environment `pipelineiq-aca-dev` | Pending | — | `pipelineiq-iac/core/container_apps.tf` | Central India |
| 7.5 | FastAPI Container App `pipelineiq-fastapi-dev` | Pending | — | Terraform | Managed identity + Key Vault refs |
| 7.6 | Ingress: public HTTPS with managed cert | Pending | — | Terraform | |

Blocker for Tier 8: 7.1–7.6 Done.

---

## Tier 8 — Dashboard & webhooks

React frontend + Azure DevOps webhook target for IaC change capture.

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 8.1 | Static Web Apps `pipelineiq-react-dev` | Pending | — | `pipelineiq-iac/core/static_web_apps.tf` | Free tier |
| 8.2 | Custom domain + managed cert | Pending | — | Terraform | Optional for dev |
| 8.3 | Azure DevOps service connection + webhook secret | Pending | — | Manual + Key Vault | Per-repo webhook → FastAPI |
| 8.4 | Event Grid → Slack webhook binding | Pending | — | `pipelineiq-iac/core/eventgrid.tf` | Alert delivery channel |

---

## Tier 9 — Verification runs

Not infrastructure — but part of the build. Confirms everything above
actually works end-to-end.

| # | Item | Status | Date | Command | Notes |
|---|---|---|---|---|---|
| 9.1 | Generator dry-run against provisioned Azure SQL | Skipped | 2026-04-21 | — | Dry-run mode has a known bug: skips catalogue INSERT then tries to `pd.read_sql` products, gets empty DF, fails in orders module. Skipped in favour of 9.2 since catalogue seed is idempotent (UUID5 deterministic). Fix in a later session. |
| 9.2 | Generator seed run (first batch) | Done | 2026-04-21 | `AZURE_SQL_PASSWORD=$(az keyvault secret show ...) .venv/bin/python generator/main.py --date 2026-01-15` | Ran in ~5:30 against live velora_oms. Catalogue (4,200 products / 45 stores / 30 reps) + day 1 data (15 customers, 308 orders [176 D2C / 41 B2B / 91 Store], 1,177 order lines) + inventory snapshot (189K rows). 2 generator bugs found + fixed mid-verification (cursor-vs-conn, fast_executemany — see DECISIONS #40 + commit `7149cb4`). |
| 9.3a | One-shot Azure SQL → landing Parquet export | Done | 2026-05-01 | `.venv/bin/python scripts/export_velora_to_landing.py --start 2026-01-15 --end 2026-01-21` | Substitutes for ADF until Tier 6 ships. 1,345,689 rows landed across 14 by-date partitions (orders, inventory_snapshot) + 8 master-table full snapshots. |
| 9.3 | ADF copy activity → landing verified | Pending | — | ADF debug run | Parquet files land in ADLS. Blocked on Tier 6 ADF Bicep. (Substituted by 9.3a for Phase 2 dev.) |
| 9.4a | First Bronze notebook smoke (`customers`) | Done | 2026-05-01 | `.venv/bin/python scripts/run_bronze_smoke.py --entity customers` | 158 rows landed in `bronze.default.customers` with all 4 audit columns + `_ingestion_date` partition. Notebook at `notebooks/bronze/ingest_to_bronze.py`. Ran on a Databricks job cluster (autoscale 1–2 workers, DS3_v2, 14.3.x-scala2.12). |
| 9.4b | Bronze backfill — all 10 entities (real-dated, post-S6 source reset) | Done | 2026-05-07 | Multi-task Job (one shared 2-worker cluster) submitted via inline `WorkspaceClient` script — each task = `bronze_{entity}` running the entity-agnostic notebook | Wiped `landing/` (54 stale Jan-2026 files), dropped `bronze.default.{customers, orders}`, re-exported 10 days (2026-04-27 → 2026-05-06) via `export_velora_to_landing.py --start ... --end ...` (1,922,920 rows), then ingested all 10 entities in parallel on one job cluster (~7 min wall: 5 min cold start + 2.5 min parallel run). Final Bronze totals: customers 248, customer_addresses 248, orders 3,619, order_lines 12,300, order_status_log 6,897, products 4,205, product_pricing 4,218, inventory_snapshot 1,891,125, sales_reps 30, territory_assignments 30. |
| 9.4c | First Silver notebook — `silver.orders` | Done | 2026-05-07 | `.venv/bin/python scripts/run_silver_smoke.py --entity orders` | Notebook at `notebooks/silver/build_silver_orders.py`. 3,619 rows in `silver.default.orders`, 100% DQ pass. 9 DQ rules with `;`-separated rejection codes, MERGE on `order_id`, partition by `_silver_date`, quarantine routing wired (no rows quarantined yet — generator produces clean OLTP data per DECISIONS #43). Pattern-setter for the rest of the Silver layer. |
| 9.4d | Second Silver notebook — `silver.customers` | Done | 2026-05-07 | `.venv/bin/python scripts/run_silver_smoke.py --entity customers` | Notebook at `notebooks/silver/build_silver_customers.py`. 248 rows in `silver.default.customers`, 100% DQ pass. 5 DQ rules. Establishes SCD-change-tracking-at-Silver pattern: `_prev_segment`, `_prev_city`, `_scd_changed` computed by left-joining incoming batch against current Silver state. |
| 9.4e | First Gold dim — `gold.dim_customer` (SCD Type 2) | Done | 2026-05-07 | `.venv/bin/python scripts/run_gold_smoke.py --entity dim_customer` | Notebook at `notebooks/gold/build_gold_dim_customer.py`. 248 dim rows on first run, all `is_current=true`. Idempotent across re-runs. SCD Type 2 on `segment` + `city`; Type 1 overwrite for other attrs. Surrogate key = `xxhash64(customer_id, valid_from)`. After SCHEMA.md refit (S8 mid-session): `valid_from = MIN(silver.orders.order_date)` per customer (earliest activity date) so as-of joins work historically — verified 3,619/3,619 silver orders match a dim row. |
| 9.4f | Silver chunk 1 — order_lines + products + product_pricing + sales_reps + territory_assignments | Done | 2026-05-09 | `.venv/bin/python scripts/run_silver_smoke.py --entity {name}` per table | All 5 notebooks under `notebooks/silver/`. Counts: order_lines 12,300; products 4,205; product_pricing 4,218; sales_reps 30; territory_assignments 30. 100% DQ pass on every row. Pattern-rep work off the S8 conventions. Verify script: `scripts/verify_silver_chunk1.py`. |
| 9.4g | Bronze extension — `product_categories` + `stores` (static seeds) | Done | 2026-05-09 | Edited `ENTITIES` in `scripts/export_velora_to_landing.py`, then `run_bronze_smoke.py --entity product_categories` + `--entity stores` | DECISIONS #60. Routes static seed tables through landing → bronze instead of JDBC-from-Gold. `bronze.default.product_categories` = 35 rows; `bronze.default.stores` = 45 rows. |
| 9.4h | Static-dims batch — dim_date + dim_sales_channel + dim_order_status + dim_product_category + dim_store | Done | 2026-05-09 | `.venv/bin/python scripts/run_gold_smoke.py --entity static_dims` | Single notebook `notebooks/gold/build_gold_static_dims.py` builds all 5 with MERGE-on-NK idempotency. Counts: dim_date 4,018 (2020-2030, FY26=365d, 5 holidays/yr), dim_sales_channel 3, dim_order_status 6, dim_product_category 35, dim_store 45 (8 territories). Verify script: `scripts/verify_gold_static_dims.py`. |
| 9.4i | Gold chunk 2 — dim_product (SCD-2) + dim_sales_rep (SCD-2) + dim_territory (synthesized) | Done | 2026-05-09 | `.venv/bin/python scripts/run_gold_smoke.py --entity {dim_product,dim_sales_rep,dim_territory}` per dim | 3 new notebooks under `notebooks/gold/`. Counts: `dim_product` 4,218 (4,205 current + 13 historical price versions; matches `silver.product_pricing` 1:1); `dim_sales_rep` 30 (no SCD changes in 12-day window); `dim_territory` 9 (8 real territories + `D2C_NATIONAL` sentinel per DECISIONS #55). All FK readiness checks clean (dim_store/dim_sales_rep territory_ids → dim_territory, dim_product category_ids → dim_product_category). Verify script: `scripts/verify_gold_chunk2.py`. SCD-2 versioning uses `LEAD(<source-effective-date>) OVER (PARTITION BY <NK> ORDER BY <effective-date>) - 1` so the dim is self-consistent regardless of source-side effective_to bookkeeping. |
| 9.4j | Gold chunk 3 — fact_order_line + fact_daily_channel_revenue | Pending | — | TBD | Keystone fact + Gold→Gold rollup. After 9.4i lands, all dims feeding `fact_order_line` are ready. SCHEMA.md `gold.fact_order_line` has the full derivation table + as-of-join SQL. |
| 9.4k | Chunk 4 — silver.inventory_snapshot + fact_inventory_daily + silver.order_status_log + silver.customer_addresses | Pending | — | TBD | Trailing-edge work. Inventory branch is the only non-trivial item (1.89M rows, partition discipline). |
| 9.4 | Bronze → Silver → Gold full run | In progress | — | Databricks Job | End-to-end Phase 2 exit. Bronze 12/12; Silver 7/10; Gold 9/12 (1+2 SCD-2 dims via 9.4e + 9.4i + 5 static dims via 9.4h + 1 synthesized dim via 9.4i). Remaining: 3 Silver + 3 facts per CLAUDE.md "Medallion chunk plan (S10 → S13)". |
| 9.5 | Inject each of 6 failure classes → verify incident row in PostgreSQL | Pending | — | `python generator/main.py --failure <class>` per class | Phase 3 exit. Requires Phase 3 infra (ADF diagnostic settings → Log Analytics → FastAPI poller → incident_store). |

---

## Quick reference: region decisions

| Resource | Region | Reason |
|---|---|---|
| Everything except Azure OpenAI | Central India | DECISIONS.md #10 — lowest latency for India devs |
| Azure OpenAI | South India | Central India does not host Azure OpenAI. See DECISIONS.md #25. Cross-region call is async RCA, latency immaterial. |

---

*Created 2026-04-21 (Session 1). Updated 2026-04-21 (Session 3, end) — Tier 2/3/4/5.1/7 all applied, bootstrap SQL complete on both PG + Azure SQL, **Phase 1 end-to-end verified** (generator seeded 2026-01-15 against live velora_oms). 30 Azure resources + first batch of real data in state. Remaining Phase 0 work: Tier 5.2-5.7 Unity Catalog (needs databricks provider), Tier 4.6 Functions app, Tier 6 ADF (Bicep). Next: (a) seed more days + inject a failure for later RCA fixtures, or (b) jump to Tier 5.2 UC + first Bronze notebook. Update the status column whenever any item changes state.*
