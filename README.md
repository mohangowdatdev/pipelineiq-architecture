# PipelineIQ

**AI-native pipeline observability and DevOps co-pilot for Azure data platforms.**

PipelineIQ monitors Azure data pipelines in real time. When a pipeline fails, it reads the IaC from the main branch, performs AI-driven root cause analysis against a pgvector index of the infrastructure, writes a plain-English incident summary, fires a Slack alert, and guides the engineer through a permission-gated fix workflow.

The AI never acts without human approval. Every step from triage to remediation requires an explicit green-light.

## Contents

- [Why this exists](#why-this-exists)
- [Demo dataset — Velora Retail Group](#demo-dataset--velora-retail-group)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Companion repos](#companion-repos)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started) — prereqs, clone, bootstrap, provision, run
- [Build phases](#build-phases)
- [Development conventions](#development-conventions)
- [Author](#author)

---

## Why this exists

Data engineering teams lose hours per incident reading logs, correlating them to Terraform, and hunting for the upstream change that broke the pipeline. PipelineIQ closes that loop:

- **Structured failure capture** — raw Azure Monitor logs → typed events in PostgreSQL within 5 minutes
- **IaC-aware root cause** — Azure DevOps webhook → IaC chunker → pgvector embeddings. When an incident fires, the relevant IaC slices are retrieved and passed to the LLM as evidence
- **Plain-English triage** — Claude/GPT generates a Slack-ready incident summary citing the exact Terraform or Bicep file responsible
- **Permission-gated remediation** — suggested fixes are proposed as PRs; humans approve every merge
- **Pattern memory** — recurring root causes are detected over time, surfacing chronic issues that look like one-off failures

---

## Demo dataset — Velora Retail Group

PipelineIQ is developed against a synthetic enterprise dataset: **Velora Retail Group**, a mid-size omnichannel retailer. The generator (this repo) emits realistic order, customer, inventory, and SCD events daily, and can inject any of 6 distinct failure classes on demand for reproducible demos.

Failure classes covered:
1. Schema drift (unexpected column added to source)
2. Volume anomaly (row count outside historical band)
3. Data quality (null violations, FK violations, type mismatches)
4. Dependency violation (out-of-order execution via control flag)
5. Watermark drift (source system clock skew)
6. Late-arriving dimension (fact refers to a dimension not yet present)

Each class maps to a distinct detection path and produces a different incident flavour downstream.

---

## Architecture

```
Azure SQL (Velora)
   └── ADF Copy Activity (watermark-based incremental)
         └── ADLS Gen2 landing/  ─► Bronze Job  ─► Silver Job  ─► Gold Jobs
                                                                    │
                                                                    ▼
                                                          Databricks SQL Warehouse
                                                          (JDBC/ODBC → VS Code, Power BI)

PostgreSQL Flexible Server (B2s + pgvector)
   ├── Relational: watermarks, file_registry, pipeline_exec_log, incident_store
   └── pgvector:   iac_embeddings (cosine similarity)

Azure DevOps (PipelineIQ-IaC)
   └── webhook ─► FastAPI chunker ─► pgvector upsert

Azure Monitor + Log Analytics
   ├── ADF diagnostic logs
   ├── Databricks diagnostic logs
   └── Container Apps logs
         └── FastAPI KQL polling ─► PostgreSQL failure_events
               └── pgvector retrieval + LLM ─► incident_store + Slack webhook
```

Medallion layers follow a strict no-back-reference rule: data only flows forward (landing → bronze → silver → gold). Bad records are quarantined with `rejection_reason` + `pipeline_run_id`, never silently dropped.

See [`PLANNING.md`](PLANNING.md) for the full architectural rationale and [`docs/`](docs/) for per-service deep dives.

---

## Tech stack

| Layer | Technology |
|---|---|
| Orchestration | Azure Data Factory |
| Source DB | Azure SQL Database (serverless, 2 vCore max) |
| Storage | ADLS Gen2 (LRS, hierarchical namespace) |
| Compute | Azure Databricks Premium (Jobs Compute) |
| SQL serving | Databricks SQL Warehouse (2X-Small Classic) |
| Control plane | PostgreSQL Flexible Server B2s + pgvector |
| Control API | Azure Functions (Python 3.11, consumption) |
| Backend | FastAPI on Azure Container Apps |
| Frontend | React on Azure Static Web Apps |
| AI model | Azure OpenAI GPT-4o (South India) |
| Observability | Azure Monitor + Log Analytics |
| IaC | Terraform + Bicep |

See [`DECISIONS.md`](DECISIONS.md) for every stack choice and its reasoning.

---

## Companion repos

This is a multi-repo project. The split is intentional — infrastructure changes have a different review and blast-radius profile than application code, and the Phase 4 IaC-webhook loop watches a single well-scoped repo.

- **[`pipelineiq-architecture`](https://github.com/mohangowdat-sail/pipelineiq-architecture)** (this repo) — specs, docs, decisions, and all application code (generator, notebooks, functions, FastAPI, React).
- **[`pipelineiq-iac`](https://github.com/mohangowdat-sail/pipelineiq-iac)** — Terraform modules and Bicep templates for every Azure resource.

Architecture is the authority. IaC defers to [`DECISIONS.md`](DECISIONS.md) in this repo for the *why*.

---

## Repository layout

```
CLAUDE.md                Session contract for any Claude Code work on this project
PLANNING.md              Full architecture, stack decisions, constraints
PROGRESS.md              Phase status, current task, session log
SCHEMA.md                Complete data model (source, bronze, silver, gold)
DECISIONS.md             Running log of every architectural decision
docs/
  build_order.md         Dependency-ordered provisioning tracker (the granular to-do)
  architecture.md        Per-phase architecture notes
  data_generation.md     Phase 1 — generator design and failure injection
  pipeline.md            Phase 2 — ADF + notebooks
  observability.md       Phase 3 — log capture + failure events
  ai_rca.md              Phase 4–5 — pgvector + LLM RCA
  api.md                 FastAPI control plane surface
  dashboard.md           React dashboard
  runbooks/              Operational runbooks (start/stop resources, inject failures)
generator/               Velora synthetic data generator
  main.py                Entry point (--date, --failure, --dry-run)
  config.py              Deterministic seed + volume knobs
  catalogue.py           Category / product / store / sales rep seeders
  customers.py           CRM writes with SCD Type 2 on segment + city
  orders.py              OMS writes (orders, order_lines, payments)
  status_updates.py      Order status lifecycle events
  dimension_changes.py   Product list_price + sales_rep territory SCD events
  failure_injector.py    6 failure classes, mutates in-memory batch before DB write
notebooks/               Databricks PySpark notebooks (bronze/silver/gold)
functions/               Azure Functions — control plane API (watermark, file registry, queue)
fastapi/                 FastAPI backend — IaC webhook, RCA orchestrator, incident store
react/                   Production dashboard (built in Phase 6)
scripts/
  bootstrap_state.sh     Run once to create Terraform state backend
  bootstrap_sql.sql      Creates the 10 Velora source tables + control_flags
  bootstrap_postgres.sql Creates PipelineIQ control plane schema + pgvector index
```

---

## Getting started

### Prerequisites

- macOS or Linux (Windows via WSL)
- Python 3.11
- Terraform ≥ 1.6
- Azure CLI, logged in to a subscription you can provision resources in
- `direnv` (optional but recommended — auto-activates the project venv + loads `.env`)

### Clone and configure

```bash
git clone https://github.com/mohangowdat-sail/pipelineiq-architecture.git
cd pipelineiq-architecture

# Python environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r generator/requirements.txt

# Environment variables
cp .env.example .env
# Edit .env — fill in your Azure subscription, tenant, SQL admin, OpenAI key, etc.
# .env is gitignored. Never commit real values.

# (optional) direnv — auto-activates venv and loads .env on cd
brew install direnv
direnv allow
```

### Bootstrap the state backend (one-time, per subscription)

```bash
bash scripts/bootstrap_state.sh
# Creates pipelineiq-rg-dev + pipelineiqtfstate + tfstate container in Central India
```

### Provision infrastructure

Infrastructure lives in the sibling repo. Clone it next to this one:

```bash
cd ..
git clone https://github.com/mohangowdat-sail/pipelineiq-iac.git
cd pipelineiq-iac/clients/velora

cp terraform.tfvars.example terraform.tfvars
# Fill in your subscription_id and tenant_id

terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

See `pipelineiq-iac/README.md` for the full provisioning sequence.

### Bootstrap databases

Once Azure SQL and PostgreSQL are provisioned:

```bash
# From pipelineiq-architecture/
sqlcmd -S $AZURE_SQL_SERVER -d velora -G -i scripts/bootstrap_sql.sql
psql $POSTGRES_URL -f scripts/bootstrap_postgres.sql
```

### Run the generator

```bash
cd generator
python main.py --date 2025-01-01 --dry-run   # Validate schema without writing
python main.py --date 2025-01-01              # Seed catalogue + first day's batch
python main.py --date 2025-01-02 --failure schema_drift  # Inject a failure
```

See [`docs/runbooks/inject_failure.md`](docs/runbooks/inject_failure.md) for all 6 failure classes.

---

## Build phases

| Phase | Status | Scope |
|---|---|---|
| 0 — Foundation | In progress | Terraform + Bicep for all core Azure resources |
| 1 — Generator | **Complete** | Synthetic Velora data + 6 failure classes |
| 2 — Pipeline | Not started | ADF + Bronze/Silver/Gold notebooks + SQL Warehouse |
| 3 — Observability | Not started | Log Analytics → structured failure events |
| 4 — pgvector + IaC webhook | Not started | IaC chunker + embedding + similarity retrieval |
| 5 — AI RCA | Not started | FastAPI orchestrator + Slack alert |
| 6 — Production dashboard | Not started | React frontend with real RCA evidence |
| 7 — Pattern memory + drift | Not started | Recurring root cause detection + portal-change alerts |
| 8 — End-to-end demo | Not started | Failure → RCA → PR → approved merge → rerun succeeds |

See [`PROGRESS.md`](PROGRESS.md) for the live tracker and session log, and [`docs/build_order.md`](docs/build_order.md) for the granular per-item provisioning status.

---

## Development conventions

- **One objective per session.** Session log format is enforced in [`CLAUDE.md`](CLAUDE.md).
- **Decisions are recorded immediately** in [`DECISIONS.md`](DECISIONS.md) — never deferred.
- **Data only flows forward** through the medallion layers. No back-references.
- **Watermarks advance only on confirmed success.** Failure retries the same window on next run.
- **The AI never auto-acts.** Every remediation requires human approval.

---

## Author

**Mohan Gowda T** — Data engineering, platform engineering, AI-assisted DevOps.
Built as a portfolio project demonstrating production-grade multi-tenant Azure data platform architecture and AI-native observability.

---

*PipelineIQ is an independent portfolio project. Velora Retail Group is a synthetic dataset, not a real company.*
