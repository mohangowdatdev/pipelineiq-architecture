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
| 3.3b | Firewall rule: local dev IP | Blocked | 2026-04-21 | `current_ip` var in `clients/velora/terraform.tfvars` | IP lookup blocked by hook; resume by user pasting IP via `! curl -s ifconfig.me` or adding via Portal/Cloud Shell |
| 3.4 | SQL admin password + connection string in Key Vault | Done | 2026-04-21 | `clients/velora/main.tf` → `azurerm_key_vault_secret` `sql-admin-password` + `sql-connection-string` | Generated via `random_password`, 24 chars |
| 3.5 | `bootstrap_sql.sql` executed against `velora_oms` | Blocked | 2026-04-21 | `sqlcmd -S pipelineiq-sql-velora-dev.database.windows.net -d velora_oms -G -i scripts/bootstrap_sql.sql` | Waits on 3.3b firewall IP or Cloud Shell run |

Blocker for Tier 5: 3.5 Done (firewall IP needed).

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
| 4.3b | Firewall rule: local dev IP | Blocked | 2026-04-21 | `current_ip` var | Same blocker as 3.3b |
| 4.4 | Postgres admin password + connection string in Key Vault | Done | 2026-04-21 | `azurerm_key_vault_secret` `postgres-admin-password` + `postgres-connection-string` | Generated via `random_password`, 24 chars; AAD admin = current user is the preferred path |
| 4.5 | `bootstrap_postgres.sql` executed against `postgres` DB | Blocked | 2026-04-21 | `psql -h pipelineiq-pg-dev.postgres.database.azure.com -U mohan.gowda@SailAnalyticsAP.onmicrosoft.com -d postgres -f scripts/bootstrap_postgres.sql` (AAD token as PGPASSWORD) | Waits on 4.3b firewall IP or Cloud Shell run |
| 4.6 | Azure Functions app `pipelineiq-functions-dev` (Python 3.11, consumption) | Pending | — | `PipelineIQ-IaC/core/functions/` | Managed identity + Key Vault reference. Module not yet written. |

Blocker for Tier 7: 4.5–4.6 Done.

---

## Tier 5 — Compute & data lakehouse (Databricks)

Databricks workspace, Unity Catalog metastore, clusters, SQL Warehouse.
Depends on Tier 2 (ADLS, Key Vault, Log Analytics) and Tier 4 (for job
secrets referenced via Key Vault).

| # | Item | Status | Date | Path / command | Notes |
|---|---|---|---|---|---|
| 5.1 | Databricks workspace `pipelineiq-dbx-dev` (Premium) | Done | 2026-04-21 | `PipelineIQ-IaC/core/databricks/main.tf` | Premium SKU, managed RG `pipelineiq-dbx-dev-managed-rg`. Workspace module deliberately minimal — UC setup deferred to 5.2. |
| 5.2 | Unity Catalog metastore attached | Pending | — | `databricks_metastore_assignment` | One per region. Needs the `databricks` provider (account-level credential) — next session |
| 5.3 | External location: ADLS containers `landing`, `bronze`, `silver`, `gold`, `quarantine` | Pending | — | `databricks_external_location` | Access via managed identity |
| 5.4 | Unity Catalog schemas: `bronze`, `silver`, `gold` | Pending | — | Notebook or `databricks_schema` | |
| 5.5 | Jobs Compute cluster policy | Pending | — | Terraform | DS3_v2, auto-terminate 30m |
| 5.6 | SQL Warehouse `pipelineiq-sqlwarehouse-dev` (2X-Small, auto-stop 10m) | Pending | — | `databricks_sql_endpoint` | Classic tier |
| 5.7 | Secret scope backed by Key Vault | Pending | — | `databricks_secret_scope` | AAD-backed |

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
| 9.1 | Generator dry-run against provisioned Azure SQL | Pending | — | `cd generator && python main.py --date 2025-01-01 --dry-run` | Verifies schema + connection |
| 9.2 | Generator seed run (first batch) | Pending | — | `python main.py --date 2025-01-01` | ~60s for catalogue + day 1 batch |
| 9.3 | ADF copy activity → landing verified | Pending | — | ADF debug run | Parquet files land in ADLS |
| 9.4 | Bronze → Silver → Gold full run | Pending | — | Databricks Job | End-to-end Phase 2 exit |
| 9.5 | Inject each of 6 failure classes → verify incident row in PostgreSQL | Pending | — | `python main.py --failure <class>` per class | Phase 3 exit |

---

## Quick reference: region decisions

| Resource | Region | Reason |
|---|---|---|
| Everything except Azure OpenAI | Central India | DECISIONS.md #10 — lowest latency for India devs |
| Azure OpenAI | South India | Central India does not host Azure OpenAI. See DECISIONS.md #25. Cross-region call is async RCA, latency immaterial. |

---

*Created 2026-04-21 (Session 1). Updated 2026-04-21 (Session 3) — Tier 2 applied in full, Tier 3/4/7 modules written + applied; 28 Azure resources live. Only blockers remaining on Phase 0: laptop-IP firewall rules on Postgres + Azure SQL, needed to run bootstrap SQL from local shell. Update the status column whenever any item changes state.*
