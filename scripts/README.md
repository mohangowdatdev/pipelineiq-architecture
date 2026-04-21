# scripts/

Bootstrap and utility scripts. Run once to provision database objects.

## Phase

Phase 0 — Foundation (bootstrap scripts)
Phase 1 — Data Generation (SQL schema bootstrap needed before generator)

## Files

| Script | When to run | What it does |
|---|---|---|
| bootstrap_state.sh | Once, before any terraform | Creates Azure Storage account for Terraform state backend |
| bootstrap_sql.sql | Once, after Azure SQL is provisioned | Creates all 4 schemas and 10 source tables in Azure SQL Database |
| bootstrap_postgres.sql | Once, after PostgreSQL is provisioned | Creates pipeline + pipelineiq schemas, all control tables, enables pgvector |

## Idempotency

All SQL scripts use IF NOT EXISTS / CREATE OR REPLACE throughout.
Safe to run multiple times — will not destroy existing data or fail on re-run.

## Run order

1. bootstrap_state.sh (before terraform)
2. terraform apply (provisions all Azure resources)
3. bootstrap_sql.sql (creates Velora source schema)
4. bootstrap_postgres.sql (creates control plane schema)
5. Run generator to seed catalogue data

## Important

bootstrap_sql.sql connects to Azure SQL Database.
bootstrap_postgres.sql connects to PostgreSQL Flexible Server.
Both scripts require their respective environment variables to be set.
