# generator/

Velora Retail Group synthetic data generator.

Runs as an Azure Function on a timer trigger at 6am daily. Generates realistic
retail transactional data across all 10 source tables in Azure SQL Database and
writes it in a single atomic transaction. Also executable as a CLI for local
testing and historical backfill.

## Phase

Phase 1 — Data Generation

## Modules

| File | Responsibility |
|---|---|
| config.py | Volume parameters, seasonal multipliers, DB connection config |
| catalogue.py | Static master data: 4,200 products, 45 stores, 30 sales reps, 8 territories |
| customers.py | Daily customer registrations and SCD Type 2 profile update events |
| orders.py | Daily order and order line generation across D2C, B2B, and Store channels |
| status_updates.py | Order status progression logic (PENDING → PROCESSING → SHIPPED → DELIVERED) |
| dimension_changes.py | Weekly price changes, monthly product launches, quarterly rep reassignments |
| failure_injector.py | Controlled bad data for all 6 PipelineIQ failure scenarios |
| main.py | Orchestrator — assembles all modules, writes to Azure SQL, Azure Function entry point |
| function.json | Azure Function timer trigger definition (6am daily) |
| host.json | Azure Functions host configuration |

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp ../.env.example .env

# Test run (generates data, validates, does not write)
python main.py --date 2025-01-15 --dry-run

# Normal run for a specific date
python main.py --date 2025-01-15

# Inject a failure scenario
python main.py --date 2025-01-15 --failure schema_drift

# Override random seed
python main.py --date 2025-01-15 --seed 999
```

## Failure scenarios

Pass `--failure` with one of: `schema_drift`, `referential_integrity`,
`volume_anomaly`, `null_constraint`, `scd_key_explosion`, `dependency_violation`

See `docs/runbooks/inject_failure.md` for full walkthrough of each scenario.
