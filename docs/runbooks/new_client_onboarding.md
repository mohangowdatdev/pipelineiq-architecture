# Runbook: Onboarding a New Client

*Populated at end of Phase 0.*

---

## Purpose

PipelineIQ is designed to be multi-client from day one. Adding a new client means
creating a new Terraform client folder, running terraform apply, and inserting their
source entities into pipeline.entity_registry. No changes to core/ or pipelineiq_app/.

This runbook documents the exact steps to onboard a new retail client after PipelineIQ
is live with Velora.

---

## Contents (to be written at end of Phase 0)

- Pre-requisites: what the new client needs to provide (source DB details, schema)
- Create clients/{client_name}/main.tf and variables.tf from the Velora template
- Terraform variables to update (resource names, connection strings, entity list)
- Run terraform plan and terraform apply
- INSERT entities into pipeline.entity_registry for all source tables
- Run bootstrap_sql.sql equivalent for the new client's schema
- Verify watermarks are initialised
- Run a test ADF pipeline execution for one entity
- Smoke test: verify data lands in landing/, bronze, silver, gold
- How PipelineIQ observability activates automatically for the new client
- IaC ingestion: how to trigger the first embedding ingest for the new client's IaC
