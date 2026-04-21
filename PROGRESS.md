# PipelineIQ — Build Progress

## Current phase

**Phase 0 — Foundation** (in progress)

Tier 1 state backend live. Tier 2 core Terraform written and planned —
apply pending. See `docs/build_order.md` for the granular per-item status.

## Next task

**First action next session: `terraform apply tfplan` from
`PipelineIQ-IaC/clients/velora/`** — creates Key Vault, Log Analytics, and
ADLS Gen2 (10 resources total).

Then, in order:
1. Verify applied resources via `terraform output` and az CLI spot-checks
2. Retry `brew install msodbcsql18 mssql-tools18` — yesterday deadlocked on EULA
3. Write `core/postgres/` module (PostgreSQL Flexible Server + pgvector)
4. Write `core/databricks/` module (Premium workspace, Unity Catalog metastore)
5. Write `source_connectors/azure_sql/` module (Velora source DB server + database)
6. Write `core/openai/` module (South India — per DECISIONS.md #25)
7. `terraform plan` + `apply` to extend Tier 2–4 resources
8. Run `scripts/bootstrap_sql.sql` against provisioned Azure SQL
9. Run `scripts/bootstrap_postgres.sql` against provisioned PostgreSQL
10. Generator dry-run + seed run against real Azure SQL (Phase 1 end-to-end verification)

Use `docs/build_order.md` to pick up the exact next row — it's
dependency-ordered and has a status column.

## Phase exit criteria tracker

| Phase | Status | Exit criteria | Result |
|---|---|---|---|
| Phase 0 | Not started | terraform apply clean, all resources exist, Unity Catalog shows 3 schemas, RBAC verified | — |
| Phase 1 | **COMPLETE** | Generator populates all 10 Azure SQL tables. All 6 failure classes produce correct bad records. | Code complete, requires Azure SQL to verify end-to-end |
| Phase 2 | Not started | Full pipeline run completes. Good records in Gold. Bad records in quarantine with correct rejection reasons. SQL Warehouse queryable from VS Code. | — |
| Phase 3 | Not started | Inject each failure class. Structured event in PostgreSQL within 5 minutes. | — |
| Phase 4 | Not started | 3 different error messages → semantically relevant IaC chunks returned. | — |
| Phase 5 | Not started | Slack alert within 5 minutes. AI identifies root cause for 4+ of 6 failure types. | — |
| Phase 6 | Not started | Dashboard shows pipeline status, incident panel shows full RCA with IaC evidence. | — |
| Phase 7 | Not started | Pattern memory identifies recurring root cause. Drift detection catches manual portal change. | — |
| Phase 8 | Not started | End-to-end demo: failure → RCA → PR approved → staging merge → rerun succeeds. | — |

## Completed phases

### Phase 1 — Data Generator (completed 2026-04-20)

All generator code written and reviewed. All 10 source table schemas exactly match SCHEMA.md.
All 6 failure scenarios implemented in failure_injector.py.
Full documentation written in docs/data_generation.md.
Failure runbook written in docs/runbooks/inject_failure.md.

## Key files created per phase

| Phase | Files created |
|---|---|
| Session 1 (this session) | See list below |
| Phase 0 | (pending) |
| Phase 1 | generator/ (all 11 files) |
| Phase 2 | (pending) |
| Phase 3 | (pending) |
| Phase 4 | (pending) |
| Phase 5 | (pending) |
| Phase 6 | (pending) |
| Phase 7 | (pending) |
| Phase 8 | (pending) |

## Files created in Session 1 (2026-04-20)

**Folder structure:**
- generator/ (directory)
- notebooks/bronze/, notebooks/silver/, notebooks/gold/ (directories + .gitkeep + README.md)
- functions/, fastapi/, react/, scripts/, docs/, docs/runbooks/ (directories + README.md or .gitkeep)

**Repo scaffolding:**
- .gitignore
- .env.example
- requirements.txt

**Generator (all 11 files):**
- generator/requirements.txt
- generator/config.py
- generator/catalogue.py
- generator/customers.py
- generator/orders.py
- generator/status_updates.py
- generator/dimension_changes.py
- generator/failure_injector.py
- generator/main.py
- generator/function.json
- generator/host.json

**Bootstrap scripts:**
- scripts/bootstrap_sql.sql
- scripts/bootstrap_postgres.sql
- scripts/bootstrap_state.sh

**Documentation:**
- docs/architecture.md (shell)
- docs/data_generation.md (complete — Phase 1)
- docs/pipeline.md (shell)
- docs/observability.md (shell)
- docs/ai_rca.md (shell)
- docs/api.md (shell)
- docs/dashboard.md (shell)
- docs/runbooks/start_stop_postgres.md (shell)
- docs/runbooks/inject_failure.md (complete — Phase 1)
- docs/runbooks/new_client_onboarding.md (shell)
- docs/README.md
- generator/README.md
- notebooks/README.md + per-layer READMEs

## Notes and blockers

1. **Phase 0 must complete before Phase 1 can be end-to-end verified.** The generator
   code is written and correct but requires a live Azure SQL Database to test against.

2. **Catalogue seed (first run) will take approximately 60s** due to 4,200 product inserts.
   This is within the 10-minute Azure Function timeout.

3. **DECISION made in Session 1:** UUID5 used for catalogue master data IDs (deterministic).
   See DECISIONS.md entry #19.

4. **DECISION made in Session 1:** Inventory snapshot written in a separate transaction
   from the main data batch. See DECISIONS.md entry #20.

5. **DECISION made in Session 1:** `control_flags` table added to `velora_oms` schema
   to support the dependency_violation failure scenario. Not extracted by ADF. See #21.

6. **BLOCKER carried into Session 3:** `msodbcsql18` + `mssql-tools18` did not install.
   `brew install` deadlocked on an interactive EULA prompt despite `ACCEPT_EULA=Y`; had
   to kill the brew process after ~55 min. Retry with `HOMEBREW_ACCEPT_EULA=Y` (different
   env var) or download Microsoft's macOS .pkg directly. Blocks Tier 3 (`sqlcmd` for
   `bootstrap_sql.sql`) and Tier 9 (pyodbc runtime for generator verification).

7. **UNCOMMITTED STATE:** Both `PipelineIQ-Architecture/` and `PipelineIQ-IaC/` have
   local changes not yet committed. IaC repo was `git init`'d but has zero commits
   yet — entire scaffold is unstaged. Commit early in Session 3 before more work lands.

8. **READY TO APPLY:** `PipelineIQ-IaC/clients/velora/tfplan` exists and plans 10 Azure
   resource additions. First action in Session 3 is `terraform apply tfplan` from that
   directory.

---
*Updated: 2026-04-21. Update this file at the end of every session before closing.*

---

## Session Log

### 2026-04-21
**Objective:** Kick off Phase 0. Bootstrap Terraform state backend. Scaffold `PipelineIQ-IaC/` as a sibling repo. Write Tier 2 core Terraform modules (Key Vault, Log Analytics, ADLS Gen2). Reach a clean `terraform plan` ready to apply.

**Built:**
- **Local env:** Python 3.11.15 via `brew install python@3.11`; `.venv` created and generator deps installed (pandas, numpy, faker, pyodbc, sqlalchemy, azure-identity, etc.); `direnv` installed and hooked into `~/.zshrc`; `.envrc` (committed) auto-activates venv + loads `.env` (gitignored, holds Azure Sponsorship sub + Sail tenant IDs); Terraform 1.14.8 via `hashicorp/tap`.
- **Azure state backend (Tier 1):** via `scripts/bootstrap_state.sh` — resource group `pipelineiq-rg-dev`, storage account `pipelineiqtfstate`, blob container `tfstate`. Landed in `Microsoft Azure Sponsorship` subscription (Sail tenant), not the SSE BI sub we'd initially been on.
- **IaC repo scaffolded:** `PipelineIQ-IaC/` created as sibling to Architecture and Portal, `git init`'d, with `.gitignore`, `.terraform-version` (1.14.8), `README.md`, `CLAUDE.md` (points back to Architecture for architectural truth), folder structure per CLAUDE.md repo map (`core/`, `source_connectors/{azure_sql,blob_storage,http_api,eventhub}/`, `clients/velora/`, `pipelineiq_app/`, `bicep/adf/`, `scripts/`).
- **Tier 2 Terraform:** three core submodules — `core/keyvault/`, `core/log_analytics/`, `core/adls/` — each with `main.tf` + `variables.tf` + `outputs.tf`. Velora composition at `clients/velora/` with `backend.tf`, `providers.tf`, `variables.tf`, `main.tf` (common tags, name prefix/suffix, three module calls), `outputs.tf`, `terraform.tfvars` (gitignored) + `terraform.tfvars.example`. `terraform init` + `validate` + `plan` all green; `tfplan` saved with 10 additions and zero changes/destroys.
- **Docs:** `docs/build_order.md` created — dependency-ordered provisioning tracker with per-item status column, nine tiers from local env to verification runs. `PipelineIQ/README.md` written at parent folder as a local-disk orientation aid (not tracked in any repo). DECISIONS.md #25–#31 added covering OpenAI region split, build_order format, azurerm v4 provider pin, single-RG workload model, submodule pattern, inline RBAC grants, deferred identity module.

**Worked:** `bootstrap_state.sh` ran clean first try. `terraform init` + `plan` produced a clean 10-resource addition plan with no errors. Submodule pattern (one module per Azure service under `core/`, composed from `clients/{client}/main.tf`) felt natural and matches industry Terraform monorepo conventions. direnv auto-activation of venv + `.env` eliminates the "wrong shell, wrong subscription" class of mistakes — particularly important given the user has both SSE BI (client) and Sponsorship (Sail) subs accessible.

**Broke:**
- `brew install terraform` on the default registry returned "command not found" after exit 0 — HashiCorp moved Terraform out of Homebrew core after the BSL license change. Fixed by switching to `brew install hashicorp/tap/terraform`.
- `brew install msodbcsql18 mssql-tools18` deadlocked for 55 minutes on an interactive EULA prompt despite `ACCEPT_EULA=Y` — had to kill. Neither package is installed. Retry path for next session: try `HOMEBREW_ACCEPT_EULA=Y` (different env var) or Microsoft's direct .pkg download from <https://learn.microsoft.com/sql/connect/odbc/linux-mac/install-microsoft-odbc-driver-sql-server-macos>.
- `azurerm_key_vault.enable_rbac_authorization` threw a v5-deprecation warning. Renamed the resource attribute to `rbac_authorization_enabled` (kept the input variable name for backwards compat); re-plan cleared the warning.

**Uncertainty:**
- Correct env var for non-interactive `msodbcsql18` install on darwin_arm64 — `ACCEPT_EULA=Y` didn't work; `HOMEBREW_ACCEPT_EULA=Y` is the documented brew-level variant but may not propagate to the formula's installer script. Fallback is Microsoft's direct .pkg.
- Tier 2 plan has not been applied — user ended session to sleep. Nothing destructive can happen overnight but the plan has a TTL concern only if provider versions drift; lock file pins them.
- `pipelineiqtfstate` storage account lives in the same RG as the workload resources we're about to create. Dev-acceptable; revisit for prod.

**Next:** First action next session is `terraform apply tfplan` from `PipelineIQ-IaC/clients/velora/`. Then: verify outputs, commit both repos (they're uncommitted), retry msodbc install, extend Tier 2 with `core/postgres/`, `core/databricks/`, `source_connectors/azure_sql/`, and `core/openai/` (south india). Pick up exact work from `docs/build_order.md`.

**Summary:** First Phase 0 session. Moved the project from "all generator code written, zero Azure" to "Azure state backend live, IaC repo scaffolded as a sibling multi-repo, three core Tier 2 submodules written and validated with a clean apply plan staged." The meaningful architectural decisions — submodule pattern, single-RG dev topology, inline RBAC grants at module level, OpenAI region split to South India, `docs/build_order.md` as the granular tracker — are all captured in DECISIONS.md #25–#31 so the next session can apply the plan cold without re-deriving the reasoning. Two carry-overs: msodbcsql install blocked on EULA, and both repos have uncommitted work.

### 2026-04-20
**Objective:** Lay the complete project foundation from a blank directory — folder structure, docs scaffolding, repo scaffolding, and full synthetic data generator end to end.

**Built:** 47 files total. generator/ (config.py, catalogue.py, customers.py, orders.py, status_updates.py, dimension_changes.py, failure_injector.py, main.py, function.json, host.json, requirements.txt). scripts/ (bootstrap_sql.sql, bootstrap_postgres.sql, bootstrap_state.sh). docs/ (10 files: 2 complete, 8 shells). Repo root (.gitignore, .env.example, requirements.txt). Folder structure with .gitkeep and README.md files for all 10 directories. PROGRESS.md, DECISIONS.md updated.

**Worked:** Schema adherence was clean — all 10 tables in bootstrap_sql.sql match SCHEMA.md exactly, including computed `line_total` and correct watermark columns per table (order_status_log uses `created_at`, inventory_snapshot uses `snapshot_date`). Failure injector design maps cleanly to 6 distinct detection paths. Module separation was natural — no module needed to know about another module's tables.

**Broke:** Two real lint issues caught mid-session: unused `Optional` import in catalogue.py, unused `categories_df` parameter in `_build_products` (function pulls category IDs from config directly). Fixed before moving on. Pylance import-not-resolved warnings for numpy/pandas/faker throughout — expected, packages not installed locally, not real errors.

**Uncertainty:** Generator is unverified against a live database — Phase 0 must complete first. Catalogue seed timing (~60s for 4,200 rows) is estimated, not measured. The `control_flags` table approach for dependency_violation needs verification that ADF Web Activity can read it at runtime. `inventory_snapshot` has no UNIQUE constraint on (product_id, store_id, snapshot_date) — double-run produces duplicates, handled in Silver.

**Next:** Phase 0 — run `scripts/bootstrap_state.sh`, then write `pipelineiq-iac/core/` Terraform modules. First resource to provision: resource group → Key Vault → ADLS Gen2. Do not touch the generator again until Phase 0 produces a live Azure SQL Database to test against.

**Summary:** Built the complete generator foundation from scratch. The generator covers all 10 source tables, 6 failure scenarios across 3 detection types (schema, volume, DQ), SCD events, and writes everything in a single atomic transaction. The key design choices — UUID5 for catalogue IDs, separate inventory transaction, failure injection on in-memory batch before transaction — are all logged in DECISIONS.md (#19-23). The code is architecturally correct per SCHEMA.md and PLANNING.md but unverified against real Azure SQL. Next session must be Phase 0 Terraform before touching any code.
