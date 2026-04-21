# PipelineIQ — Data Generation

*Written end of Phase 1. Generator fully built and verified.*

---

## What the generator does

The generator is a Python application that runs at 6am every day as an Azure Function.
It produces realistic synthetic operational data for Velora Retail Group and writes it
into Azure SQL Database across 10 source tables in 4 schemas. ADF picks up that data
at the next pipeline run and moves it through the medallion layers.

The generator serves a specific purpose in PipelineIQ's architecture: it gives us a
data source we fully control. Every column, every relationship, every edge case is
deterministic and intentional. We can inject specific failure scenarios on demand and
know exactly what they should look like downstream.

---

## Why synthetic data instead of a public dataset

Public datasets like NYC Taxi or Chicago Taxi were evaluated and rejected.
They have no order lifecycle, no SCD events, no referential integrity across tables,
no channel split (D2C/B2B/Store), and no narrative that resonates with an enterprise
audience. Velora Retail Group's data has all of these things. Every table in the schema
reflects a real decision a retail company makes: should I track segment upgrades as
SCD Type 2? (Yes — because you need to know whether a customer was VIP when they made
a purchase, not whether they're VIP today.) Does a Store POS order carry a rep_id?
(No — it's a walk-in sale, no rep is assigned.)

---

## Velora Retail Group business context

Mid-market Indian omnichannel retailer. Rs. 850 crore/year revenue.
Three channels: D2C ecommerce, B2B wholesale (200+ resellers), 45 physical stores.
4,200 active SKUs across 5 divisions. Headquarters: Bangalore.
The business context explains every modelling decision in the schema.

---

## Module responsibilities

| Module | Responsibility |
|---|---|
| config.py | All configuration: volume ranges, seasonal multipliers, DOW multipliers, city list, division definitions, price ranges, DB connection builder |
| catalogue.py | Static master data: 4,200 products, 45 stores, 30 sales reps, 8 territories. Generated deterministically with UUID5 IDs. Seeded once on first run. |
| customers.py | Daily customer registrations (INSERTs) and SCD Type 2 update events (UPDATEs to segment/city — detected by Silver as SCD changes) |
| orders.py | Daily order generation: D2C (150-300/day), B2B (20-50/day), Store POS (80-150/day). All with order_lines. Referential integrity enforced before write. |
| status_updates.py | Advances existing orders through PENDING → PROCESSING → SHIPPED → DELIVERED. 2% of DELIVERED orders generate RETURN_INITIATED. |
| dimension_changes.py | Weekly price changes (every Monday, 3-8 products). Monthly product launches (1st of month, 5-10 SKUs). Quarterly rep reassignments (quarter start, 1-2 reps). |
| failure_injector.py | Controlled bad data injection. Accepts a failure_type flag. Implements all 6 failure scenarios. Operates on the in-memory batch before any DB writes. |
| main.py | Orchestrator. Calls all modules in order. Writes everything in a single transaction. Supports --dry-run, --date, --failure, --seed CLI flags. Azure Function entry point. |

---

## Daily volumes

Base volumes before multipliers:

| Metric | Range | Notes |
|---|---|---|
| D2C orders | 150-300/day | Multiplied by DOW and seasonal factors |
| B2B orders | 20-50/day | Same multipliers |
| Store POS orders | 80-150/day | Same multipliers |
| Status progressions | 200-400/day | Acts on existing open orders |
| New customer registrations | 15-40/day | INSERTs to customers table |
| Customer SCD updates | 5-15/day | UPDATEs to customers; SCD handled in Silver |
| Inventory snapshot | 189,000 rows/day | Full refresh: 4,200 products × 45 stores |
| Price changes | 3-8/week | Mondays only |
| Product launches | 5-10/month | 1st of month only |
| Rep territory changes | 1-2/quarter | Quarter start only |

### Day-of-week multipliers

| Day | Multiplier | Rationale |
|---|---|---|
| Monday | 1.00 | Baseline |
| Tuesday | 1.00 | Baseline |
| Wednesday | 1.05 | Mid-week pickup |
| Thursday | 1.10 | Pre-payday |
| Friday | 1.35 | Payday + weekend-start shopping |
| Saturday | 1.25 | Weekend shopping |
| Sunday | 0.70 | Lower demand |

### Seasonal multipliers

November and December are 2.0x (Diwali, Black Friday, year-end clearance).
October is 1.2x (Navratri/Dussehra). January is 0.85x (post-festival dip).

---

## Running locally for testing

### Prerequisites

```bash
# Create a virtual environment
python -m venv venv && source venv/bin/activate

# Install dependencies
pip install -r generator/requirements.txt

# Copy and fill in the .env file
cp .env.example .env
# Set AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD
```

### Test run (does not write to DB)

```bash
cd generator
python main.py --date 2025-01-15 --dry-run
```

Expected output: a summary of record counts per table. No DB writes.

### Normal run for a specific date

```bash
cd generator
python main.py --date 2025-01-15
```

First run will seed the catalogue (~4,200 products, 45 stores, 30 reps).
This takes approximately 30-60 seconds. Subsequent runs are faster.

### Backfill historical data

To generate data for a range of dates (e.g., for a demo dataset):

```bash
for date in $(seq -f "%04g-01-%02g" 2025 1 30); do
    python main.py --date "2025-01-$date"
done
```

Or write a simple loop:

```bash
python -c "
from datetime import date, timedelta
import subprocess, sys
start = date(2025, 1, 1)
end = date(2025, 4, 1)
d = start
while d < end:
    subprocess.run([sys.executable, 'main.py', '--date', str(d)], check=True)
    d += timedelta(days=1)
"
```

### Override seed for different data variants

```bash
python main.py --date 2025-01-15 --seed 999
```

Different seeds produce different names, prices, and distributions but the
same structural shape and referential integrity.

---

## Injecting failure scenarios

```bash
python main.py --date 2025-01-15 --failure schema_drift
python main.py --date 2025-01-15 --failure referential_integrity
python main.py --date 2025-01-15 --failure volume_anomaly
python main.py --date 2025-01-15 --failure null_constraint
python main.py --date 2025-01-15 --failure scd_key_explosion
python main.py --date 2025-01-15 --failure dependency_violation
```

For the full step-by-step demo runbook, see `docs/runbooks/inject_failure.md`.

---

## Azure Function deployment

The generator is deployed as an Azure Function with a timer trigger.
Schedule: `0 0 6 * * *` (6am UTC daily).

```
generator/
  function.json   — timer trigger definition
  host.json       — function host config (Python worker, 10min timeout)
  main.py         — entry point: main(mytimer) called by Functions runtime
  *.py            — generator modules
  requirements.txt — Python dependencies
```

Deployment method: Azure Functions Core Tools or ZIP deploy via Azure DevOps CI.

The function timeout is set to 10 minutes to accommodate:
- Azure SQL serverless auto-resume (~5-15s if server was paused)
- Catalogue seeding on first run (~60s for 4,200 product inserts)
- Inventory snapshot writes (~30s for 189,000 rows)

---

## Known constraints and limitations

1. **inventory_snapshot does not have a UNIQUE constraint on (product_id, store_id, snapshot_date).**
   Running the generator twice for the same date produces duplicate snapshot rows.
   This is intentional for simplicity — deduplicated in the Silver layer.
   If exact idempotency is needed, add the constraint and use INSERT OR IGNORE in the generator.

2. **Customer email uniqueness is not enforced.**
   The generator produces realistic-looking emails but does not check for duplicates.
   Azure SQL has no UNIQUE constraint on email. For a production system, add this constraint.

3. **The dependency_violation failure scenario writes a control flag to velora_oms.control_flags.**
   This table is not extracted by ADF and is not in the Gold model. It exists solely to
   support the failure injection mechanism. The flag must be manually cleared after the demo.

4. **B2B order_lines do not carry a territory_id.**
   Territory is derived from the assigned rep's current territory assignment in the Gold layer.
   This is a join, not a stored value.

5. **The generator is designed for Azure SQL Database. It will not run against Azure SQL
   Managed Instance without modifying the connection string.**
