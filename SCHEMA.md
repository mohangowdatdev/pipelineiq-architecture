# PipelineIQ — Schema Reference

Read this file before writing any notebook, SQL query, ADF dataset,
or data-related code. It is the authoritative column-level reference
for every table in the architecture.

---

## Azure SQL Database — Velora source tables

Most transactional tables carry these audit columns:
  created_at    DATETIME2 DEFAULT GETUTCDATE()
  updated_at    DATETIME2 DEFAULT GETUTCDATE()
  source_system NVARCHAR(20) DEFAULT 'VELORA_{SCHEMA}'

Static reference tables (`product_categories`, `stores`, `control_flags`)
carry a reduced set — they are seeded once by the generator and are **not**
extracted by ADF. See the per-table specs below and the `## Schema change
log` at the bottom of this file for the rationale.

ADF watermark column: updated_at (most tables); exceptions — `order_status_log`
uses `created_at`, `inventory_snapshot` uses `snapshot_date`, `product_pricing`
and `territory_assignments` use `created_at` (append-only by design).

### velora_oms.orders
```
order_id        NVARCHAR(36)  PK
customer_id     NVARCHAR(36)  FK -> velora_crm.customers
channel_type    NVARCHAR(20)  -- 'D2C' | 'B2B' | 'STORE'
order_date      DATE
status          NVARCHAR(30)  -- 'PENDING'|'PROCESSING'|'SHIPPED'|
                              --  'DELIVERED'|'CANCELLED'|'RETURNED'
store_id        NVARCHAR(20)  NULL (only for STORE channel)
rep_id          NVARCHAR(36)  NULL (only for B2B channel)
total_amount    DECIMAL(12,2)
currency        NVARCHAR(3)   DEFAULT 'INR'
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_oms.order_lines
```
line_id         NVARCHAR(36)  PK
order_id        NVARCHAR(36)  FK -> velora_oms.orders
product_id      NVARCHAR(36)  FK -> velora_pim.products
quantity        INT
unit_price      DECIMAL(10,2)
discount_amt    DECIMAL(10,2) DEFAULT 0
line_total      DECIMAL(12,2) COMPUTED (quantity * unit_price - discount_amt)
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_oms.order_status_log
```
log_id          NVARCHAR(36)  PK
order_id        NVARCHAR(36)  FK -> velora_oms.orders
from_status     NVARCHAR(30)
to_status       NVARCHAR(30)
changed_at      DATETIME2
changed_by      NVARCHAR(50)  -- 'SYSTEM' | 'AGENT' | user_id
created_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_oms.control_flags
```
flag_id         INT           PK IDENTITY(1,1)
flag_name       NVARCHAR(100)
flag_value      NVARCHAR(100)
set_at          DATETIME2     DEFAULT GETUTCDATE()
```

Operational control table. Read by ADF Web Activity at runtime to toggle
execution paths (e.g. `force_early_fact_run` for the `dependency_violation`
failure scenario). **Not extracted by ADF.** Not in Bronze/Silver/Gold.
Added per DECISIONS #21.

### velora_crm.customers
```
customer_id     NVARCHAR(36)  PK
full_name       NVARCHAR(200)
email           NVARCHAR(200)
phone           NVARCHAR(20)  NULL
segment         NVARCHAR(30)  -- 'INDIVIDUAL' | 'BUSINESS' | 'VIP'
city            NVARCHAR(100)
state           NVARCHAR(100)
account_type    NVARCHAR(20)  -- 'D2C' | 'B2B'
is_active       BIT           DEFAULT 1
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_crm.customer_addresses
```
address_id      NVARCHAR(36)  PK
customer_id     NVARCHAR(36)  FK -> velora_crm.customers
address_line    NVARCHAR(500)
city            NVARCHAR(100)
state           NVARCHAR(100)
pincode         NVARCHAR(10)
is_primary      BIT           DEFAULT 0
address_type    NVARCHAR(20)  -- 'HOME' | 'WORK' | 'BILLING' | 'SHIPPING'
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_pim.product_categories
```
category_id     NVARCHAR(36)  PK
category_name   NVARCHAR(200)
sub_category    NVARCHAR(200) NULL
division        NVARCHAR(50)
is_active       BIT           DEFAULT 1
```

Static reference table. 35 rows seeded once by `generator/catalogue.py`.
No audit columns (by design — never updated post-seed). **Not extracted
by ADF** — loaded directly into `gold.dim_product_category`.

### velora_pim.products
```
product_id      NVARCHAR(36)  PK
sku             NVARCHAR(50)  UNIQUE
product_name    NVARCHAR(500)
category_id     NVARCHAR(36)  FK -> velora_pim.product_categories
division        NVARCHAR(50)  -- 'CONSUMER_ELECTRONICS' | 'HOME_APPLIANCES' |
                              --  'PERSONAL_CARE' | 'SPORTS_FITNESS' |
                              --  'PREMIUM_ACCESSORIES'
brand           NVARCHAR(100)
is_active       BIT           DEFAULT 1
launched_date   DATE
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_pim.product_pricing
```
pricing_id      NVARCHAR(36)  PK
product_id      NVARCHAR(36)  FK -> velora_pim.products
list_price      DECIMAL(10,2)
cost_price      DECIMAL(10,2)
effective_from  DATE
effective_to    DATE          NULL (NULL = currently active)
pricing_type    NVARCHAR(20)  -- 'STANDARD' | 'PROMOTIONAL' | 'CLEARANCE'
created_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_pim.inventory_snapshot
```
snapshot_id     NVARCHAR(36)  PK
product_id      NVARCHAR(36)  FK -> velora_pim.products
store_id        NVARCHAR(20)  FK -> velora_pim.stores
snapshot_date   DATE
opening_stock   INT
units_sold      INT           DEFAULT 0
units_returned  INT           DEFAULT 0
closing_stock   INT
stockout_flag   BIT           DEFAULT 0
reorder_point   INT
created_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_pim.stores
```
store_id        NVARCHAR(20)  PK
store_name      NVARCHAR(200)
city            NVARCHAR(100)
state           NVARCHAR(100)
territory_id    NVARCHAR(36)
store_tier      NVARCHAR(20)  -- 'FLAGSHIP' | 'STANDARD' | 'EXPRESS'
is_active       BIT           DEFAULT 1
opened_date     DATE          NULL
```

Static store master. 45 rows seeded once by `generator/catalogue.py`.
No audit columns. **Not extracted by ADF** — loaded directly into
`gold.dim_store`. Referenced by `velora_oms.orders.store_id` (STORE
channel only) and `velora_pim.inventory_snapshot.store_id`.

Added to SCHEMA.md and to `seed_to_db` mid-Session 4 after a data-quality
sweep caught 45 missing rows; see DECISIONS #44 and the change log below.

### velora_hrm.sales_reps
```
rep_id          NVARCHAR(36)  PK
full_name       NVARCHAR(200)
email           NVARCHAR(200)
phone           NVARCHAR(20)  NULL
hire_date       DATE
is_active       BIT           DEFAULT 1
created_at      DATETIME2
updated_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_hrm.territory_assignments
```
assignment_id   NVARCHAR(36)  PK
rep_id          NVARCHAR(36)  FK -> velora_hrm.sales_reps
territory_id    NVARCHAR(36)
assigned_from   DATE
assigned_to     DATE          NULL (NULL = currently assigned)
is_current      BIT           DEFAULT 1
created_at      DATETIME2
source_system   NVARCHAR(20)
```

### velora_hrm.territories (does not exist)

There is **no** `velora_hrm.territories` source table. `territory_id`
values appear as uncorrelated strings in `velora_pim.stores` and
`velora_hrm.territory_assignments`. `gold.dim_territory` is built from
the distinct set of those strings plus a city/state/region enrichment
lookup in the Gold notebook — not from a source extract.

`generator/catalogue.py::_build_territories()` is dead code. See
DECISIONS #44 tail.

---

## Bronze layer — Delta tables

All Bronze tables mirror their source exactly plus these audit columns:
```
_source_file        STRING    -- ADLS path of the source Parquet file
_ingestion_timestamp TIMESTAMP -- when ADF wrote the file to landing
_pipeline_run_id    STRING    -- ADF pipeline run ID (GUID)
_bronze_timestamp   TIMESTAMP -- when Databricks wrote this row to bronze
```

Bronze tables (10): bronze.orders, bronze.order_lines,
bronze.order_status_log, bronze.customers, bronze.customer_addresses,
bronze.products, bronze.product_pricing, bronze.inventory_snapshot,
bronze.sales_reps, bronze.territory_assignments

Partition: all Bronze tables partition by _ingestion_timestamp::DATE

---

## Silver layer — conformed Delta tables

### silver.orders
```
order_id        STRING    PK (business key for MERGE)
customer_id     STRING
channel_type    STRING    -- 'D2C' | 'B2B' | 'STORE' (unified)
order_date      DATE
status          STRING
store_id        STRING    NULL
rep_id          STRING    NULL
total_amount    DECIMAL(12,2)
currency        STRING
-- DQ flags
dq_passed       BOOLEAN
dq_rejection_reason STRING NULL
-- Audit
_source_file    STRING
_pipeline_run_id STRING
_ingestion_timestamp TIMESTAMP
_silver_timestamp TIMESTAMP
```

### silver.order_lines
```
line_id         STRING    PK
order_id        STRING
product_id      STRING
quantity        INT
unit_price      DECIMAL(10,2)
discount_amt    DECIMAL(10,2)
line_total      DECIMAL(12,2)
dq_passed       BOOLEAN
dq_rejection_reason STRING NULL
_source_file    STRING
_pipeline_run_id STRING
_silver_timestamp TIMESTAMP
```

### silver.customers
```
customer_id     STRING    PK
full_name       STRING
email           STRING
segment         STRING
city            STRING
state           STRING
account_type    STRING
is_active       BOOLEAN
-- SCD change tracking (set before writing to Gold)
_prev_segment   STRING    NULL
_prev_city      STRING    NULL
_scd_changed    BOOLEAN
_source_file    STRING
_pipeline_run_id STRING
_silver_timestamp TIMESTAMP
```

(Remaining Silver tables follow the same pattern — business key,
business columns, DQ flags, SCD change tracking where applicable,
and audit columns. Full column lists for all Silver tables are
generated during Phase 2 and appended here.)

---

## Gold layer — star schema Delta tables

### gold.dim_customer
```
surrogate_key       BIGINT    PK (auto-generated hash or sequence)
customer_id         STRING    NK (natural key)
full_name           STRING
email               STRING
segment             STRING
city                STRING
state               STRING
account_type        STRING    -- 'D2C' | 'B2B'
channel_type        STRING
is_active           BOOLEAN
valid_from          DATE
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_product
```
surrogate_key       BIGINT    PK
product_id          STRING    NK
sku                 STRING
product_name        STRING
division            STRING
brand               STRING
category_id         STRING
is_active           BOOLEAN
list_price          DECIMAL(10,2)  -- SCD Type 2 tracked
cost_price          DECIMAL(10,2)
valid_from          DATE
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_product_category
```
category_id         STRING    PK
category_name       STRING
sub_category        STRING    NULL
division            STRING
is_active           BOOLEAN
```

### gold.dim_sales_channel
```
channel_id          STRING    PK
channel_name        STRING
channel_type        STRING    -- 'D2C' | 'B2B' | 'STORE'
description         STRING
```

### gold.dim_sales_rep
```
surrogate_key       BIGINT    PK
rep_id              STRING    NK
full_name           STRING
email               STRING
territory_id        STRING    -- SCD Type 2 tracked
is_active           BOOLEAN
valid_from          DATE
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_territory
```
territory_id        STRING    PK
territory_name      STRING
city                STRING
state               STRING
region              STRING
is_active           BOOLEAN
_pipeline_run_id    STRING
```

### gold.dim_order_status
```
status_id           STRING    PK
status_name         STRING
status_category     STRING    -- 'ACTIVE' | 'CLOSED' | 'EXCEPTION'
sort_order          INT
```

### gold.dim_date
```
date_id             INT       PK (YYYYMMDD format)
full_date           DATE
day_of_week         STRING
day_number          INT
week_number         INT
month_number        INT
month_name          STRING
quarter             INT
year                INT
fiscal_year         INT       -- Indian fiscal (Apr-Mar)
fiscal_quarter      INT
is_weekend          BOOLEAN
is_public_holiday   BOOLEAN
holiday_name        STRING    NULL
```

### gold.dim_store
```
store_id            STRING    PK
store_name          STRING
city                STRING
state               STRING
territory_id        STRING
store_tier          STRING    -- 'FLAGSHIP' | 'STANDARD' | 'EXPRESS'
is_active           BOOLEAN
opened_date         DATE      NULL
_pipeline_run_id    STRING
```

### gold.fact_order_line
```
order_line_id           STRING    PK (surrogate)
order_id                STRING    NK
line_id                 STRING    NK
-- Foreign keys
order_date_id           INT       FK -> dim_date
customer_surrogate_key  BIGINT    FK -> dim_customer
product_surrogate_key   BIGINT    FK -> dim_product
channel_id              STRING    FK -> dim_sales_channel
rep_surrogate_key       BIGINT    FK -> dim_sales_rep (NULL for D2C/Store)
store_id                STRING    FK -> dim_store (NULL for D2C/B2B)
territory_id            STRING    FK -> dim_territory
status_id               STRING    FK -> dim_order_status
-- Measures
quantity_ordered        INT
unit_price_at_sale      DECIMAL(10,2)  -- from dim_product at time of sale
discount_amount         DECIMAL(10,2)
line_total_inr          DECIMAL(12,2)
tax_amount              DECIMAL(10,2)
net_revenue_inr         DECIMAL(12,2)
-- Audit
_pipeline_run_id        STRING
_ingestion_timestamp    TIMESTAMP
_gold_timestamp         TIMESTAMP
```

### gold.fact_daily_channel_revenue
```
-- Grain: channel + product_category + date
summary_date_id         INT       FK -> dim_date
channel_id              STRING    FK -> dim_sales_channel
category_id             STRING    FK -> dim_product_category
territory_id            STRING    FK -> dim_territory
-- Measures
total_orders            INT
total_lines             INT
total_units_sold        INT
gross_revenue_inr       DECIMAL(14,2)
total_discount_inr      DECIMAL(14,2)
net_revenue_inr         DECIMAL(14,2)
avg_order_value_inr     DECIMAL(10,2)
return_rate_pct         DECIMAL(5,2)
-- Audit
_pipeline_run_id        STRING
_gold_timestamp         TIMESTAMP
```

### gold.fact_inventory_daily
```
-- Grain: product + store + date
snapshot_date_id        INT       FK -> dim_date
product_surrogate_key   BIGINT    FK -> dim_product
store_id                STRING    FK -> dim_store
-- Measures
opening_stock           INT
units_sold              INT
units_returned          INT
closing_stock           INT
stockout_flag           BOOLEAN
reorder_point           INT
days_of_stock_remaining INT       NULL (closing_stock / avg_daily_sales)
-- Audit
_pipeline_run_id        STRING
_gold_timestamp         TIMESTAMP
```

---

## Quarantine tables

### quarantine.orders
```
quarantine_id       STRING    PK (auto-generated UUID)
rejection_reason    STRING    -- e.g. 'UNKNOWN_CUSTOMER_ID', 'NULL_REQUIRED_FIELD'
pipeline_run_id     STRING
rejected_at         TIMESTAMP
raw_record          STRING    -- JSON-serialised original record
source_table        STRING    -- 'velora_oms.orders'
```

### quarantine.order_lines
```
quarantine_id       STRING    PK
rejection_reason    STRING
pipeline_run_id     STRING
rejected_at         TIMESTAMP
raw_record          STRING
source_table        STRING    -- 'velora_oms.order_lines'
```

---

## PostgreSQL control plane tables

### pipeline.entity_registry
```
entity_id           SERIAL    PK
entity_name         VARCHAR(100)  -- 'velora_oms.orders'
source_schema       VARCHAR(50)   -- 'velora_oms'
source_table        VARCHAR(100)  -- 'orders'
watermark_column    VARCHAR(50)   -- 'updated_at'
load_type           VARCHAR(20)   -- 'incremental' | 'full'
schedule            VARCHAR(20)   -- 'daily' | 'weekly' | 'monthly'
active              BOOLEAN       DEFAULT true
priority            INT           DEFAULT 5
depends_on          VARCHAR(100)  NULL -- entity_name of dependency
```

### pipeline.watermarks
```
watermark_id        SERIAL    PK
entity_name         VARCHAR(100)
environment         VARCHAR(20)   DEFAULT 'dev'
last_successful_load TIMESTAMPTZ
next_window_start   TIMESTAMPTZ
updated_at          TIMESTAMPTZ   DEFAULT NOW()
```

### pipeline.process_queue
```
queue_id            SERIAL    PK
entity_name         VARCHAR(100)
scheduled_run_time  TIMESTAMPTZ
status              VARCHAR(20)   -- 'pending'|'running'|'complete'|'failed'
retry_count         INT           DEFAULT 0
last_heartbeat      TIMESTAMPTZ   NULL
run_id              VARCHAR(100)  NULL
created_at          TIMESTAMPTZ   DEFAULT NOW()
```

### pipeline.file_registry
```
file_id             SERIAL    PK
file_path           TEXT
source_entity       VARCHAR(100)
landed_at           TIMESTAMPTZ
row_count           INT
pipeline_run_id     VARCHAR(100)
processed_flag      BOOLEAN       DEFAULT false
processed_at        TIMESTAMPTZ   NULL
```

### pipeline.pipeline_exec_log
```
log_id              SERIAL    PK
run_id              VARCHAR(100)
pipeline_name       VARCHAR(200)
entity_name         VARCHAR(100)  NULL
start_time          TIMESTAMPTZ
end_time            TIMESTAMPTZ   NULL
status              VARCHAR(20)   -- 'running'|'success'|'failed'
rows_read           INT           NULL
rows_written        INT           NULL
rows_rejected       INT           NULL
error_message       TEXT          NULL
created_at          TIMESTAMPTZ   DEFAULT NOW()
-- Append-only. Never UPDATE or DELETE.
```

### pipelineiq.incident_store
```
incident_id         UUID      PK DEFAULT gen_random_uuid()
pipeline_id         VARCHAR(200)
failure_timestamp   TIMESTAMPTZ
root_cause_summary  TEXT
affected_component  VARCHAR(200)
evidence            JSONB     -- array of evidence strings
suggested_fix       TEXT
confidence          VARCHAR(10)   -- 'high'|'medium'|'low'
iac_chunks_used     JSONB     -- array of {file_path, resource_type, content}
raw_logs_used       TEXT
created_at          TIMESTAMPTZ   DEFAULT NOW()
-- Append-only. Never UPDATE or DELETE.
```

### pipelineiq.iac_embeddings
```
id                  SERIAL    PK
file_path           TEXT
resource_type       VARCHAR(100)  -- 'azurerm_data_factory_pipeline' etc
branch              VARCHAR(100)  DEFAULT 'main'
content             TEXT          -- original Terraform/Bicep block
embedding           vector(1536)  -- pgvector column
ingested_at         TIMESTAMPTZ   DEFAULT NOW()
```

Index: `CREATE INDEX ON pipelineiq.iac_embeddings
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`

---

*This file is updated during Phase 2 (Silver columns) and as the
schema evolves. Always read this before writing data code.*

---

## Schema change log

Append-only record of every schema edit. New rows on top. Never edit
or remove prior rows — superseded entries get a "superseded by #N" note.

### #3 — 2026-04-22 (Session 4) — `velora_hrm.territories` clarified as non-existent

- **Change:** added explicit "does not exist" note after `territory_assignments`.
- **Why:** `generator/catalogue.py::_build_territories()` produces a DataFrame
  that is never written anywhere — `bootstrap_sql.sql` has no `territories`
  table. Territory IDs live only as strings in `stores` and `territory_assignments`.
  `gold.dim_territory` is synthesised in the Gold notebook from the distinct set
  of those strings. SCHEMA.md previously implied a territories table existed
  (because `dim_territory` has a full spec under Gold) — now explicit.
- **Generator impact:** `_build_territories()` is dead code. Flagged for cleanup
  but not load-bearing.
- **Reference:** DECISIONS #44 (tail).

### #2 — 2026-04-22 (Session 4) — `velora_pim.stores` added to SCHEMA.md

- **Change:** added full source-table spec (previously only the Gold
  `dim_store` was documented).
- **Why:** the table was present in `bootstrap_sql.sql` and `bootstrap_sql.sql`
  created it, but SCHEMA.md never listed it as a source. Consequence: the
  generator's `seed_to_db` originally **skipped** the stores INSERT because
  no spec forced the author to remember it (`_build_stores()` built the
  DataFrame, `build_catalogue()` returned it, but the write step never
  referenced it). 45 stores sat in memory and never landed. Bug caught by a
  data-quality sweep at the end of Session 4 task 1 — 770 STORE-channel
  orders and 1.32M inventory rows pointed at `store_id` values with no row
  in `velora_pim.stores`. Silver/Gold joins would have been empty.
- **Generator impact:** `catalogue.py::seed_to_db` now writes `stores` after
  `product_pricing` and before `sales_reps`. Backfilled 45 rows in-session.
- **Pipeline impact:** `stores` is **static** — not extracted by ADF, loaded
  directly into `gold.dim_store` from the source seed. No Bronze/Silver pass.
- **Reference:** DECISIONS #44.

### #1 — 2026-04-22 (Session 4) — `velora_pim.product_categories` and `velora_oms.control_flags` given full specs

- **Change:** `product_categories` was previously a parenthetical "static ref
  table" on `products.category_id`; now has a full column spec. `control_flags`
  was undocumented here entirely (only mentioned in DECISIONS #21).
- **Why (`product_categories`):** 35 rows seeded by the generator, needed for
  `gold.dim_product_category`. Any notebook author would have to reverse-engineer
  the column set from `bootstrap_sql.sql` — cheaper to document here once.
- **Why (`control_flags`):** operational control table read by ADF Web Activity
  at runtime. Not a pipeline-extracted entity, but SCHEMA.md is the canonical
  column-level reference for every table that exists in the source DB, so it
  belongs here with a clear "not in Bronze/Silver/Gold" note to prevent a future
  notebook author from accidentally ingesting it.
- **Generator impact:** none — both tables were already written correctly.
- **Reference:** DECISIONS #21 (for `control_flags`).
