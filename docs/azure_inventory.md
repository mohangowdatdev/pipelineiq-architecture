# Azure Inventory — Live Deployment Snapshot

**Snapshot date:** 2026-04-22 (Session 4, start)
**Subscription:** Microsoft Azure Sponsorship
**Resource group:** `pipelineiq-rg-dev`
**Tenant:** Sail Analytics AP

This file answers one question: **what is actually deployed in Azure right now?**

Update every time resources are added, removed, or change region. Discrepancy
between this file and Portal = this file wrong. PROGRESS.md's "30 resources"
figure and `docs/build_order.md` statuses are both derived from this snapshot.

---

## Portal-visible top-level resources (9)

These are what appears in the Azure Portal Resource Group listing.
Sub-resources (filesystems, secrets, role assignments, firewall rules, AAD admins,
model deployments, server parameters) are nested under these parents and are
**not** visible at this level — they're enumerated in the next section.

| # | Name | Type | Region | Module |
|---|---|---|---|---|
| 1 | `pipelineiqtfstate` | Storage account | Central India | `scripts/bootstrap_state.sh` (Tier 1) |
| 2 | `pipelineiq-kv-dev` | Key Vault | Central India | `core/keyvault/` (Tier 2) |
| 3 | `pipelineiq-logs-dev` | Log Analytics workspace | Central India | `core/log_analytics/` (Tier 2) |
| 4 | `pipelineiqadlsdev` | Storage account (ADLS Gen2) | Central India | `core/adls/` (Tier 2) |
| 5 | `pipelineiq-pg-dev` | Postgres Flexible Server | Central India | `core/postgres/` (Tier 4) |
| 6 | `pipelineiq-sql-velora-dev` | SQL server | Central India | `source_connectors/azure_sql/` (Tier 3) |
| 7 | `velora_oms` (under `pipelineiq-sql-velora-dev`) | SQL database | Central India | `source_connectors/azure_sql/` (Tier 3) |
| 8 | `pipelineiq-dbx-dev` | Azure Databricks Service | Central India | `core/databricks/` (Tier 5.1) |
| 9 | `pipelineiq-openai-dev` | Azure OpenAI | **South India** | `core/openai/` (Tier 7) |

Region split reason: Azure OpenAI is not offered in Central India.
See DECISIONS.md #25.

---

## Full Terraform state inventory (~30 resources)

Breakdown of the "30 resources" count. Sub-resources nested under parents above.

### Under `pipelineiq-kv-dev` (Key Vault — 1 + 2 RBAC + 6 secrets = 9)

- `azurerm_key_vault.this` (parent)
- `azurerm_role_assignment.current_user_kv_secrets_officer` — grants running principal Key Vault Secrets Officer
- `azurerm_role_assignment.current_user_kv_administrator` — grants running principal Key Vault Administrator
- Secrets:
  - `postgres-admin-password`
  - `postgres-connection-string`
  - `sql-admin-password`
  - `sql-connection-string`
  - `openai-api-key`
  - `openai-endpoint`

### Under `pipelineiqadlsdev` (ADLS Gen2 — 1 + 1 RBAC + 5 filesystems = 7)

- `azurerm_storage_account.this` (parent, HNS enabled)
- `azurerm_role_assignment.current_user_storage_blob_owner` — grants running principal Storage Blob Data Owner
- Filesystems (containers with hierarchical namespace):
  - `landing/`
  - `bronze/`
  - `silver/`
  - `gold/`
  - `quarantine/`

### Under `pipelineiq-pg-dev` (Postgres Flex — 1 + 1 config + 2 firewall + 1 AAD admin = 5)

- `azurerm_postgresql_flexible_server.this` (B2s, PG 16, 32 GB, zone 1)
- `azurerm_postgresql_flexible_server_configuration.azure_extensions` = `vector,pg_trgm,uuid-ossp` (lowercase — DECISIONS #37)
- `azurerm_postgresql_flexible_server_firewall_rule.allow_azure_services` (0.0.0.0–0.0.0.0)
- `azurerm_postgresql_flexible_server_firewall_rule.current_ip` (69.5.168.130)
- `azurerm_postgresql_flexible_server_active_directory_administrator.current_user` — AAD admin = `mohan.gowda@SailAnalyticsAP.onmicrosoft.com`

### Under `pipelineiq-sql-velora-dev` (Azure SQL — 1 server + 1 DB + 2 firewall + 1 AAD admin = 5)

- `azurerm_mssql_server.this` (v12.0, TLS 1.2 min)
- `azurerm_mssql_database.velora_oms` (GP_S_Gen5_2 serverless, auto-pause 60 min)
- `azurerm_mssql_firewall_rule.allow_azure_services`
- `azurerm_mssql_firewall_rule.current_ip`
- `azurerm_mssql_server_microsoft_support_auditing_policy` / AAD admin = current user

### Under `pipelineiq-dbx-dev` (Databricks — 1)

- `azurerm_databricks_workspace.this` (Premium, managed RG `pipelineiq-dbx-dev-managed-rg`)

### Under `pipelineiq-openai-dev` (OpenAI — 1 account + 1 deployment = 2)

- `azurerm_cognitive_account.this` (kind=OpenAI, S0, South India)
- `azurerm_cognitive_deployment.this["gpt-4o"]` (model version `2024-11-20`, capacity 10)

### Under `pipelineiq-logs-dev` (Log Analytics — 1)

- `azurerm_log_analytics_workspace.this` (PerGB2018, 30-day retention)

### Tier 1 state backend (outside velora composition — ~3)

- `pipelineiq-rg-dev` (resource group, manually created by `bootstrap_state.sh`)
- `pipelineiqtfstate` (storage account for tf state)
- `tfstate` (blob container)

**Total ≈ 30 resources**, depending on exact counting of RBAC/firewall/admin
sub-resources. Matches the figure cited in PROGRESS.md.

---

## Data initialised inside these resources

- **Key Vault `pipelineiq-kv-dev`:** 6 secrets (listed above).
- **Postgres `pipelineiq-pg-dev/postgres`:** `pipeline` + `pipelineiq` schemas, 6 control tables (`entity_registry`, `watermarks`, `process_queue`, `file_registry`, `pipeline_exec_log`, `incident_store`), pgvector extension + `iac_embeddings` table with ivfflat index, 10 rows seeded into `entity_registry` (one per source table).
- **Azure SQL `velora_oms`:** 4 schemas (`crm`, `oms`, `catalog`, `ops`), 11 base tables + `control_flags`. Real Velora data for 2026-01-15: 4,200 products, 45 stores, 30 reps, 15 customers, 308 orders, 1,177 order lines, 189,000 inventory snapshot rows (via `generator/main.py` run in Session 3).
- **ADLS `pipelineiqadlsdev`:** 5 empty filesystems. No files written yet — waiting on ADF (Tier 6) or one-shot export.
- **Databricks `pipelineiq-dbx-dev`:** workspace only. No Unity Catalog metastore, no external locations, no catalogs, no clusters, no SQL Warehouse. Needs Tier 5.2–5.7.
- **OpenAI `pipelineiq-openai-dev`:** `gpt-4o` deployment live, not yet called from any production code in this repo. (Portal demo uses a separate `pipeline-iq-resource` AIServices account — see DECISIONS #34.)

---

## Expected absences (not yet provisioned, on purpose)

All of these are Pending in `docs/build_order.md`. Their absence is not a bug.

| Tier | Missing resource | Reason |
|---|---|---|
| 4.6 | `pipelineiq-functions-dev` (Azure Functions, Python 3.11, consumption) | Module not yet written. Blocker for Phase 4+. |
| 5.2–5.7 | Unity Catalog metastore, external locations, catalogs, Jobs Compute policy, SQL Warehouse, secret scope | Needs the `databricks` Terraform provider (account-level auth). Separate second-stage apply. |
| 6.1–6.6 | Data Factory + linked services + datasets + copy pipeline | Bicep not yet written. Will populate `landing/` from Azure SQL. |
| 7.4–7.6 | Container Apps environment + FastAPI container app + ingress | Phase 5 infra. |
| 8.1–8.4 | Static Web Apps, Event Grid → Slack webhook binding | Phase 6–8 infra. |

---

## Separate deployments (not in this RG)

Non-Architecture Azure resources that exist but aren't part of this build:

- **`pipeline-iq-resource`** (Azure AI Services, `rg-pipelineiq`, centralus, Sponsorship sub) — backs the Portal live demo's `/api/generate-incident` + `/api/chat-incident`. Pre-existing from Portal session work, **not managed by Terraform**, not part of PipelineIQ-IaC. See DECISIONS #34 for full details and the canonical `az` retrieve command.

All other Azure resources on the Sponsorship subscription should be assumed
**unrelated to PipelineIQ** unless explicitly listed in this file.

---

*Update this file whenever:*
- *A Terraform apply adds/removes resources in the velora composition.*
- *A sub-resource is added (new KV secret, new filesystem, new firewall rule, new model deployment).*
- *A bootstrap script seeds or tears down data inside any of the above.*
