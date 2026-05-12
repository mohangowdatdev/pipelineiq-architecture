# PipelineIQ — Schema Reference

Read this file before writing any notebook, SQL query, ADF dataset,
or data-related code. It is the authoritative column-level reference
for every table in the architecture.

## Contents

- [Data lifecycle, frequency, volume](#data-lifecycle-frequency-volume) — generator cadence, evolution patterns, volume forecast
- [Azure SQL Database — Velora source tables](#azure-sql-database--velora-source-tables) — 12 source tables across 4 schemas
- [Bronze layer — Delta tables](#bronze-layer--delta-tables) — 12 entity-agnostic append-only tables
- [Silver layer — conformed Delta tables](#silver-layer--conformed-delta-tables) — 10 dedup'd + DQ-validated tables
- [Gold layer — star schema Delta tables](#gold-layer--star-schema-delta-tables) — 9 dims + 3 facts
- [Quarantine tables](#quarantine-tables) — 1 per Silver entity, lazily created
- [PostgreSQL control plane tables](#postgresql-control-plane-tables) — 5 operational + 2 observability
- [Schema change log](#schema-change-log) — append-only, newest first

## At a glance (current state, 2026-05-11)

| Layer | Tables | Status | Volume reference (14 days) |
|---|---|---|---|
| Source (Azure SQL `velora_oms`) | 12 | Live, 14 real-dated days 2026-04-27 → 2026-05-10 | ~2.7M rows total (inventory dominates) |
| Bronze (`bronze.default.*`) | 12 | All hydrated | append-only; multiple ingestion waves accumulate |
| Silver (`silver.default.*`) | 10 | All built, 100% DQ pass on current data | 2,648,025 in `inventory_snapshot`; <20K in everything else combined |
| Gold dims (`gold.default.dim_*`) | 9 | All built | 8 dims with surrogates; `dim_date` covers 2020-2030 |
| Gold facts (`gold.default.fact_*`) | 3 | All built | `fact_order_line`, `fact_daily_channel_revenue`, `fact_inventory_daily` |
| Quarantine (`quarantine.default.*`) | 0 active | Routing wired on every Silver | Lazily created on first DQ failure |

---

## Data lifecycle, frequency, volume

How data enters the source DB, how often, and how much accumulates per
unit time. **Read this before reasoning about retention, partitioning, or
performance.** Per-table column specs live further down.

### How the source is fed

The Velora OLTP DB is fed by `generator/main.py` — a manual command-line
script invoked **once per logical date**:

```bash
.venv/bin/python generator/main.py --date 2026-01-15
```

There is no automation today. The 7 days currently in `velora_oms`
(2026-01-15 through 2026-01-21) reflect 7 manual invocations across
Sessions 3 and 4. A half-built Azure Function deployment hook
(`generator/function.json` + `host.json`) would let it run on a schedule
once deployed; that work is a Phase 0 loose end (see PROGRESS.md). To
extend the dataset, fire the script for each new date.

### Four data-evolution patterns at the source

Each Velora table follows one of four patterns. Pipeline design
(SCD-Type, MERGE keys, partitioning, quarantine logic) flows from this
classification.

#### 1. Append-only event tables — immutable history

Each row is a discrete event. Once written, never updated. Re-running
the generator for the same date is idempotent (deterministic UUIDs from
date-derived RNG seed per DECISIONS #41).

| Table | Event | Daily rows (typical) |
|---|---|---|
| `velora_oms.orders` | A new order placed that day | ~340 |
| `velora_oms.order_lines` | Line items for those orders | ~1,200 |
| `velora_oms.order_status_log` | A status transition (`PENDING → PROCESSING` → …) | ~470 |
| `velora_pim.product_pricing` | A price-change record (with `effective_from` + `effective_to`) | sporadic, ~6 over 7 days |
| `velora_hrm.territory_assignments` | A sales-rep territory change | sporadic |

Querying `orders WHERE order_date = 2026-01-15` always returns the same
308 rows. ADF watermark for these in production: `updated_at` (or
`created_at` for the append-only-by-design ones), but the generator's
audit columns currently hold seed time, not logical date — see
DECISIONS #47 for why the export script (`scripts/export_velora_to_landing.py`)
pivots on business-date columns instead.

#### 2. Mutable dimension tables — UPDATEd in place, prior values lost at source

Rows are inserted on first appearance, then **mutated** when the entity
changes. The source DB does not preserve history — if customer X's
segment changes from `INDIVIDUAL` to `BUSINESS` on day 18, the source
row is overwritten and the prior segment is gone from `velora_crm.customers`.

| Table | Mutations |
|---|---|
| `velora_crm.customers` | New customers added daily; `segment`, `city`, `is_active` may change for existing rows |
| `velora_crm.customer_addresses` | New addresses added; `is_primary` flag flips |
| `velora_pim.products` | New launches; `is_active` flips (rare) |
| `velora_hrm.sales_reps` | New onboards; minor field updates |

Daily mutation volume is small (~3–10 changes/day per table) — driven
by `generator/dimension_changes.py`. **This is where SCD-Type-2 logic in
Gold earns its keep:** Silver captures the prior value before each MERGE
so `gold.dim_customer` keeps both rows with `valid_from`/`valid_to`/
`is_current` and can answer "what was the segment on Jan 16?". Without
that capture, source mutation = irreversible loss.

#### 3. Daily full-snapshot table — `velora_pim.inventory_snapshot`

The architectural outlier. Every run captures a **full state of every
product × every store × that date**:

```
4,200 products × 45 stores = 189,000 rows per day
```

Each row reports `opening_stock`, `units_sold`, `units_returned`,
`closing_stock`, `stockout_flag`, `reorder_point` for that
`(product_id, store_id, snapshot_date)` triple. Day N's snapshot is
**independent** of day N-1's — there is no event log connecting them.
`gold.fact_inventory_daily` reads these directly.

ADF watermark: `snapshot_date` (DATE, not DATETIME2 — see per-table
spec below).

#### 4. Static reference tables — seeded once, never updated

| Table | Why static |
|---|---|
| `velora_pim.stores` | 45 stores, fixed retail footprint |
| `velora_pim.product_categories` | 35 entries, fixed taxonomy |
| `velora_oms.control_flags` | Operational toggles (e.g. `force_early_fact_run`), not business data |

These are loaded by `generator/catalogue.py::seed_to_db` on first run
and **not extracted by ADF** — Gold loads them directly from the source
seed. Listed here only so future notebook authors don't accidentally
ingest them.

### Volume forecast

Worst-case (`inventory_snapshot`) projections — all other tables
combined add <1% of the inventory volume.

| Horizon | Inventory rows | Compressed Parquet | All tables combined |
|---|---:|---:|---:|
| 7 days (today) | 1,323,000 | ~31 MB | ~33 MB |
| 30 days | 5,670,000 | ~135 MB | ~145 MB |
| 90 days | 17,010,000 | ~405 MB | ~430 MB |
| 365 days | 68,985,000 | ~1.6 GB | ~1.7 GB |

Bytes per row measured at ~23 bytes (snappy-compressed Parquet, observed
on day-15 file: 4.44 MB / 189,000 rows). Storage cost on ADLS at
$0.018/GB/month: a full year of data = **~3¢/month**. Storage is not
the constraint at any horizon we'd realistically build to.

Query performance: with date partitioning + Z-ORDER on
`(product_id, store_id)` at Gold, point-in-time queries stay fast even
on the full year. Aggregate scans across 12 months on a 2X-Small SQL
warehouse complete in ~10-30 seconds — fine for analytics, slow for
operational dashboards (use cached aggregates if the dashboard needs
sub-second).

### Real-world scale comparison

Velora's volume is **small** by retail standards. Reference points:

| Retailer (rough) | SKUs × stores | Inventory rows/day | rows/year |
|---|---|---:|---:|
| Velora (this project) | 4,200 × 45 | 189K | 69M |
| Mid-size India retailer (Croma, Westside) | ~30K × ~200 | 6M | 2.2B |
| Walmart-scale | ~50K × ~4,700 | 235M | 86B |

Daily-snapshot inventory is the standard pattern at every scale —
real warehouses use date-partitioned Parquet on S3/ADLS exactly the way
we are. The architecture scales linearly; only the partition / Z-order /
warehouse-size knobs need turning.

### Operational guidance for PipelineIQ specifically

This is a demo project. **Don't backfill beyond 60-90 days.** A year of
data adds no demonstrable value — every chart, SCD example, failure
scenario, and RCA narrative works on 30 days. Beyond ~90 days, query
times during dev get noticeably slower and cluster minutes add up
without changing what you can show.

When you do extend the dataset:
```bash
for d in $(seq 22 31); do
  .venv/bin/python generator/main.py --date 2026-01-$(printf %02d $d)
done
```

**If you ever needed to scale beyond 90 days** in a real deployment,
the architectural lever is **CDC (delta) instead of full snapshot** —
record only inventory *changes* (units_sold, restocks, returns) and
reconstruct state by replay. Trades storage for compute. Real warehouses
typically do **both** — CDC for transactional truth, daily snapshots for
fast point-in-time queries. PipelineIQ stays on snapshots-only because
the demo narrative is "look at end-of-day state" — adding CDC would
increase complexity without changing what we demonstrate.

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
status          NVARCHAR(30)  -- 'PENDING'|'PROCESSING'|'SHIPPED'|'DELIVERED'|
                              --  'CANCELLED'|'RETURN_INITIATED'|'RETURNED'
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
No audit columns (by design — never updated post-seed). Routed
landing → bronze → `gold.dim_product_category` (no Silver hop) per
DECISIONS #60.

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
No audit columns. Routed landing → bronze → `gold.dim_store` (no Silver
hop) per DECISIONS #60. Referenced by `velora_oms.orders.store_id`
(STORE channel only) and `velora_pim.inventory_snapshot.store_id`.

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
_source_file         STRING    -- ADLS path of the source Parquet file
_ingestion_timestamp TIMESTAMP -- when ADF wrote the file to landing
_pipeline_run_id     STRING    -- ADF pipeline run ID (GUID)
_bronze_timestamp    TIMESTAMP -- when Databricks wrote this row to bronze
_ingestion_date      DATE      -- partition column = to_date(_ingestion_timestamp)
```

Bronze tables (12), all under the `bronze` catalog, `default` schema:
`bronze.default.{orders, order_lines, order_status_log, customers,
customer_addresses, products, product_pricing, inventory_snapshot,
sales_reps, territory_assignments, product_categories, stores}`.

The last two (`product_categories`, `stores`) are static reference tables
routed through landing → bronze per DECISIONS #60; they still bypass
Silver and feed `gold.dim_product_category` + `gold.dim_store` directly.

Partition: all Bronze tables partition by `_ingestion_date` (a derived
DATE, not the raw TIMESTAMP — partitioning on a TIMESTAMP creates
high-cardinality partitions).

Bronze is **append-only and entity-agnostic** — one notebook
(`notebooks/bronze/ingest_to_bronze.py`) handles all 12 entities,
parameterised by `entity_name`. No business logic, no dedup, no DQ.
Schema drift is intentionally tolerated via `mergeSchema = true`.

---

## Silver layer — conformed Delta tables

### Conventions for all Silver tables

Every Silver table follows the same shape. Per-entity specs below only
list the **business columns** explicitly — the conventions in this
section apply uniformly.

**Audit columns (on every row):**
```
_source_file         STRING     ADLS path of the source Parquet (from Bronze)
_pipeline_run_id     STRING     ADF run ID (random UUID for manual runs)
_ingestion_timestamp TIMESTAMP  when Bronze landed the row
_silver_timestamp    TIMESTAMP  when Silver wrote the row
_silver_date         DATE       partition column, = to_date(_silver_timestamp)
```

**DQ flags (on every row):**
```
dq_passed             BOOLEAN
dq_rejection_reason   STRING NULL    `;`-separated rejection codes when dq_passed=false
```

**Operational rules:**
- **Dedup** by business key (PK), keeping the latest `_bronze_timestamp` row.
- **MERGE on business key** — Silver writes are idempotent; re-running on
  the same Bronze state is a no-op.
- **Partition** by `_silver_date`.
- **Bad rows route to `quarantine.default.{entity}`** with the full
  rejection-reason string and the original record JSON-serialised in
  `raw_record`.

**Documented per-table deviations from these conventions:**
- `silver.inventory_snapshot` partitions by `snapshot_date` (NOT
  `_silver_date`) — DECISIONS #63. Volume + downstream pruning needs.
- `silver.inventory_snapshot` dedups on the composite
  `(product_id, store_id, snapshot_date)` (NOT just the PK `snapshot_id`)
  — re-extracted dates would otherwise produce duplicate (product, store,
  date) rows with different `snapshot_id`s.
- `silver.order_status_log` `INVALID_STATUS` DQ rule allows 7 states
  (the 6 in `gold.dim_order_status` + `RETURN_INITIATED` that the
  generator emits as a transitional state) — DECISIONS #64.

**SCD-change tracking (only on tables whose Gold dim has SCD-2 attrs):**
- `_prev_<attr>` columns capture the about-to-be-overwritten value.
- `_scd_changed` BOOLEAN marks any row whose tracked attrs changed.
- These columns drive Gold's SCD-2 close-old/insert-new logic; the
  comparison logic lives in Silver, the SCD state machine lives in Gold.

### silver.orders

Source: `velora_oms.orders` (append-only event table at source).

```
order_id        STRING        PK (business key for MERGE)
customer_id     STRING
channel_type    STRING        -- 'D2C' | 'B2B' | 'STORE'
order_date      DATE
status          STRING
store_id        STRING NULL   -- only for STORE channel
rep_id          STRING NULL   -- only for B2B channel
total_amount    DECIMAL(12,2)
currency        STRING
```

DQ rules: `NULL_ORDER_ID`, `NULL_CUSTOMER_ID`, `UNKNOWN_CUSTOMER_ID`
(FK to `silver.customers`), `INVALID_CHANNEL_TYPE`, `NULL_ORDER_DATE`,
`NULL_STATUS`, `INVALID_TOTAL_AMOUNT` (≤ 0), `STORE_MISSING_STORE_ID`,
`B2B_MISSING_REP_ID`.

### silver.order_lines

Source: `velora_oms.order_lines` (append-only).

```
line_id         STRING        PK
order_id        STRING
product_id      STRING
quantity        INT
unit_price      DECIMAL(10,2)
discount_amt    DECIMAL(10,2)
line_total      DECIMAL(12,2) -- = quantity * unit_price - discount_amt (source-computed)
```

DQ rules: `NULL_LINE_ID`, `NULL_ORDER_ID`, `NULL_PRODUCT_ID`,
`UNKNOWN_ORDER_ID` (FK to `silver.orders`), `UNKNOWN_PRODUCT_ID` (FK to
`silver.products`), `INVALID_QUANTITY` (≤ 0), `INVALID_UNIT_PRICE` (≤ 0).

### silver.order_status_log

Source: `velora_oms.order_status_log` (append-only event log).

```
log_id          STRING        PK
order_id        STRING
from_status     STRING NULL   -- NULL on the first transition
to_status       STRING
changed_at      TIMESTAMP
changed_by      STRING        -- 'SYSTEM' | 'AGENT' | user_id
```

DQ rules: `NULL_LOG_ID`, `NULL_ORDER_ID`, `NULL_TO_STATUS`,
`UNKNOWN_ORDER_ID` (FK), `INVALID_STATUS` (`from_status`/`to_status` not
in known set).

### silver.customers

Source: `velora_crm.customers` (mutable dim — UPDATEd in place at source).

```
customer_id     STRING        PK
full_name       STRING
email           STRING
segment         STRING        -- 'INDIVIDUAL' | 'BUSINESS' | 'VIP'
city            STRING
state           STRING
account_type    STRING        -- 'D2C' | 'B2B'
is_active       BOOLEAN
-- SCD change tracking (drives gold.dim_customer SCD-2)
_prev_segment   STRING NULL
_prev_city      STRING NULL
_scd_changed    BOOLEAN
```

DQ rules: `NULL_CUSTOMER_ID`, `INVALID_EMAIL` (NULL or no `@`),
`INVALID_SEGMENT`, `INVALID_ACCOUNT_TYPE`, `NULL_FULL_NAME`.

`phone` from source is **dropped at Silver** — not used by any Gold
entity; carrying it through adds nullable noise.

### silver.customer_addresses

Source: `velora_crm.customer_addresses` (mutable — `is_primary` flips,
new addresses added).

```
address_id      STRING        PK
customer_id     STRING
address_line    STRING
city            STRING
state           STRING
pincode         STRING
is_primary      BOOLEAN
address_type    STRING        -- 'HOME' | 'WORK' | 'BILLING' | 'SHIPPING'
```

DQ rules: `NULL_ADDRESS_ID`, `NULL_CUSTOMER_ID`, `UNKNOWN_CUSTOMER_ID`
(FK), `INVALID_ADDRESS_TYPE`, `NULL_PINCODE`.

No SCD tracking — Gold doesn't carry historical address versions.

### silver.products

Source: `velora_pim.products` — mutable dim **attributes only**.
`list_price` lives in `silver.product_pricing` (separate table — the
source preserves price history as an event log, and Silver mirrors that).

```
product_id      STRING        PK
sku             STRING
product_name    STRING
category_id     STRING
division        STRING        -- 5 known divisions
brand           STRING
is_active       BOOLEAN
launched_date   DATE
```

DQ rules: `NULL_PRODUCT_ID`, `NULL_SKU`, `INVALID_DIVISION` (not in
5 known), `NULL_CATEGORY_ID`.

No SCD tracking on this table — `list_price` is the only SCD-2 attr in
`gold.dim_product`, and it's sourced from `silver.product_pricing`.

### silver.product_pricing

Source: `velora_pim.product_pricing` (append-only price-change log).

```
pricing_id      STRING        PK
product_id      STRING
list_price      DECIMAL(10,2)
cost_price      DECIMAL(10,2)
effective_from  DATE
effective_to    DATE NULL     -- NULL = currently active
pricing_type    STRING        -- 'STANDARD' | 'PROMOTIONAL' | 'CLEARANCE'
```

DQ rules: `NULL_PRICING_ID`, `NULL_PRODUCT_ID`, `UNKNOWN_PRODUCT_ID`
(FK to `silver.products`), `INVALID_LIST_PRICE` (≤ 0),
`INVALID_PRICING_TYPE`, `NULL_EFFECTIVE_FROM`.

Used by `gold.dim_product` for SCD-2 list-price tracking and by
`gold.fact_order_line` for `unit_price_at_sale` cross-checks.

### silver.inventory_snapshot

Source: `velora_pim.inventory_snapshot` (daily full snapshot — 189K
rows/day).

```
snapshot_id     STRING        PK
product_id      STRING
store_id        STRING
snapshot_date   DATE
opening_stock   INT
units_sold      INT
units_returned  INT
closing_stock   INT
stockout_flag   BOOLEAN
reorder_point   INT
```

Dedup is on the composite **(`product_id`, `store_id`, `snapshot_date`)**
— the `snapshot_id` PK already enforces uniqueness, but a re-extracted
date should not produce two Silver rows for the same (product, store, date).

DQ rules: `NULL_SNAPSHOT_ID`, `NULL_PRODUCT_ID`, `NULL_STORE_ID`,
`NULL_SNAPSHOT_DATE`, `UNKNOWN_PRODUCT_ID` (FK), `UNKNOWN_STORE_ID` (FK
to source seed `velora_pim.stores`), `NEGATIVE_STOCK` (any of opening /
closing < 0).

### silver.sales_reps

Source: `velora_hrm.sales_reps` — mutable dim **attributes only**.
`territory_id` lives in `silver.territory_assignments` (the source
preserves territory history as an event log).

```
rep_id          STRING        PK
full_name       STRING
email           STRING
phone           STRING NULL
hire_date       DATE
is_active       BOOLEAN
```

DQ rules: `NULL_REP_ID`, `INVALID_EMAIL`, `NULL_FULL_NAME`,
`NULL_HIRE_DATE`.

No SCD tracking on this table — `territory_id` is the only SCD-2 attr
in `gold.dim_sales_rep`, and it's sourced from
`silver.territory_assignments`.

### silver.territory_assignments

Source: `velora_hrm.territory_assignments` (append-only assignment log).

```
assignment_id   STRING        PK
rep_id          STRING
territory_id    STRING
assigned_from   DATE
assigned_to     DATE NULL     -- NULL = currently assigned
is_current      BOOLEAN
```

DQ rules: `NULL_ASSIGNMENT_ID`, `NULL_REP_ID`, `NULL_TERRITORY_ID`,
`UNKNOWN_REP_ID` (FK to `silver.sales_reps`), `NULL_ASSIGNED_FROM`.

Used by `gold.dim_sales_rep` for SCD-2 territory tracking.

---

## Gold layer — star schema Delta tables

### Conventions for all Gold tables

- **Surrogate key** on every dim with SCD-2: `xxhash64(<NK>, valid_from)`
  — deterministic, stateless, no sequence required. Re-runs produce
  identical keys.
- **Audit columns** on every Gold row: `_pipeline_run_id` STRING,
  `_gold_timestamp` TIMESTAMP. Facts also carry `_ingestion_timestamp`
  (pulled through from Silver) for end-to-end lineage.
- **`valid_from` for first-time SCD-2 dim rows** = earliest known
  activity date for that natural key (e.g. `MIN(orders.order_date)` for
  a customer; `MIN(product_pricing.effective_from)` for a product). If
  no activity exists yet, fall back to the source `created_at::date`.
  This makes as-of joins work correctly for historical orders placed
  *before* the dim row was first written.
- **`valid_to` on close-out** = `current_date() - 1` (the previous
  version's last valid day is yesterday; the new version's first valid
  day is today).
- **Fact → dim joins use as-of pattern** for SCD-2 dims:
  ```sql
  JOIN dim ON fact.<NK> = dim.<NK>
   AND fact.<event_date> BETWEEN dim.valid_from
                              AND COALESCE(dim.valid_to, '9999-12-31')
  ```
  Always join to all rows of the dim (not just `is_current = true`) so
  historical facts attribute to the correct historical dim version.
- **Static dims** (`dim_product_category`, `dim_sales_channel`,
  `dim_order_status`, `dim_date`, `dim_store`, `dim_territory`) bypass
  Silver and load directly from source seed or a hardcoded list in the
  Gold notebook. They have no `valid_from`/`valid_to`/`is_current`
  columns.

### gold.dim_customer

SCD Type 2 on `segment`, `city`. SCD Type 1 (overwrite in place) on
`full_name`, `email`, `state`, `account_type`, `is_active`,
`channel_type`.

`channel_type` is **derived from `account_type`** (D2C → D2C, B2B → B2B).
A D2C customer can place STORE orders, but the *customer* doesn't have a
STORE channel attribute — that's a fact-level concern.

```
surrogate_key       BIGINT    PK (xxhash64(customer_id, valid_from))
customer_id         STRING    NK
full_name           STRING
email               STRING
segment             STRING    -- SCD-2
city                STRING    -- SCD-2
state               STRING
account_type        STRING    -- 'D2C' | 'B2B'
channel_type        STRING    -- derived = account_type
is_active           BOOLEAN
valid_from          DATE
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_product

SCD Type 2 on `list_price`. SCD Type 1 on all other attrs.

**Source path:** Gold joins `silver.products` (mutable dim attrs) +
`silver.product_pricing` (price history) at build time. Each new pricing
row in `silver.product_pricing` (where `effective_to IS NULL`) that
differs from the dim's current `list_price` triggers a SCD-2 split:
  - `valid_from` on the new dim row = `product_pricing.effective_from`
  - `valid_to` on the closed dim row = `product_pricing.effective_from - 1`

This makes the SCD-2 timeline *match the source's price-effective-date
timeline*, not Gold's processing date — important because as-of joins
on `fact_order_line.order_date` rely on the dim timeline matching real
business activity.

```
surrogate_key       BIGINT    PK (xxhash64(product_id, valid_from))
product_id          STRING    NK
sku                 STRING
product_name        STRING
division            STRING
brand               STRING
category_id         STRING
is_active           BOOLEAN
list_price          DECIMAL(10,2)  -- SCD-2 tracked, sourced from silver.product_pricing
cost_price          DECIMAL(10,2)
pricing_type        STRING         -- carries through from silver.product_pricing
valid_from          DATE      -- = silver.product_pricing.effective_from
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_product_category

SCD Type 0 (static). Loaded directly from source seed
`velora_pim.product_categories` — bypasses Silver per the static-dim
convention.

```
category_id         STRING    PK
category_name       STRING
sub_category        STRING    NULL
division            STRING
is_active           BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_sales_channel

SCD Type 0 (static). 3 hardcoded rows in the Gold notebook — no source
table.

```
channel_id          STRING    PK    -- 'D2C' | 'B2B' | 'STORE' (matches channel_type)
channel_name        STRING          -- 'Direct to Consumer' | 'Business to Business' | 'Physical Store'
channel_type        STRING          -- redundant with channel_id; kept for query ergonomics
description         STRING
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_sales_rep

SCD Type 2 on `territory_id`. SCD Type 1 on all other attrs.

**Source path:** Gold joins `silver.sales_reps` (mutable dim attrs) +
`silver.territory_assignments` (assignment history) at build time. Each
new assignment row that supersedes the dim's current `territory_id`
triggers a SCD-2 split:
  - `valid_from` on the new dim row = `territory_assignments.assigned_from`
  - `valid_to` on the closed dim row = `territory_assignments.assigned_from - 1`

Same principle as `dim_product` — the SCD timeline matches source
assignment dates, not Gold processing dates.

```
surrogate_key       BIGINT    PK (xxhash64(rep_id, valid_from))
rep_id              STRING    NK
full_name           STRING
email               STRING
territory_id        STRING    -- SCD-2 tracked, sourced from silver.territory_assignments
is_active           BOOLEAN
valid_from          DATE      -- = silver.territory_assignments.assigned_from
valid_to            DATE      NULL = currently active
is_current          BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_territory

SCD Type 1. Synthesized in the Gold notebook from the **distinct set of
`territory_id` values** in `gold.dim_store` (which is sourced from
`bronze.default.stores` per DECISIONS #60) + `silver.territory_assignments`,
plus a city/state/region enrichment lookup hardcoded in the notebook
(matches `generator/config.py::CITIES`). Any observed `territory_id`
not present in the lookup is written with NULL city/state and
`region = 'UNKNOWN'` so dashboards never drop rows; the missing keys
are logged at notebook runtime as the prompt to extend the lookup.

**One sentinel row added explicitly: `D2C_NATIONAL`.** D2C orders have
no store and no rep, so no natural territory; the sentinel keeps
`fact_order_line.territory_id` NOT NULL and lets every territory
aggregation sum cleanly without filtering.

```
territory_id        STRING    PK     -- includes 'D2C_NATIONAL' sentinel
territory_name      STRING
city                STRING NULL      -- NULL for D2C_NATIONAL
state               STRING NULL
region              STRING           -- 'NATIONAL' for D2C_NATIONAL
is_active           BOOLEAN
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_order_status

SCD Type 0 (static). 7 hardcoded rows in the Gold notebook — 6 terminal/active
states plus `RETURN_INITIATED`, the transitional state the generator emits
between DELIVERED and RETURNED (DECISIONS #64). Mirrors the valid-status
set enforced by `silver.order_status_log` so a future
`fact_order_status_transitions` has a clean FK target.

```
status_id           STRING    PK    -- matches velora_oms.orders.status values + RETURN_INITIATED
status_name         STRING
status_category     STRING          -- 'ACTIVE' | 'CLOSED' | 'EXCEPTION'
sort_order          INT
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

Hardcoded values:
- `PENDING`          / Pending          / ACTIVE    / 1
- `PROCESSING`       / Processing       / ACTIVE    / 2
- `SHIPPED`          / Shipped          / ACTIVE    / 3
- `DELIVERED`        / Delivered        / CLOSED    / 4
- `CANCELLED`        / Cancelled        / EXCEPTION / 5
- `RETURN_INITIATED` / Return Initiated / EXCEPTION / 6
- `RETURNED`         / Returned         / EXCEPTION / 7

### gold.dim_date

SCD Type 0 (static). Generated once for `2020-01-01` → `2030-12-31` by a
Python helper in the Gold notebook. Not extracted from any source.

```
date_id             INT       PK (YYYYMMDD format, e.g. 20260115)
full_date           DATE
day_of_week         STRING
day_number          INT
week_number         INT
month_number        INT
month_name          STRING
quarter             INT
year                INT
fiscal_year         INT       -- Indian fiscal (Apr-Mar): FY26 = 2026-04-01..2027-03-31
fiscal_quarter      INT
is_weekend          BOOLEAN
is_public_holiday   BOOLEAN
holiday_name        STRING    NULL
_pipeline_run_id    STRING
_gold_timestamp     TIMESTAMP
```

### gold.dim_store

SCD Type 1 (static — store master rarely changes). Loaded directly from
source seed `velora_pim.stores` — bypasses Silver per the static-dim
convention.

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
_gold_timestamp     TIMESTAMP
```

### gold.fact_order_line

Grain: one row per SKU per order. PK is **pass-through `line_id`** from
source — already a UUID and globally unique, no synthesis needed.

**FK lookup pattern — as-of join on `order_date`** for SCD-2 dims:
```sql
JOIN dim_customer dc ON sol.customer_id = dc.customer_id
                    AND so.order_date BETWEEN dc.valid_from
                                          AND COALESCE(dc.valid_to, '9999-12-31')
JOIN dim_product  dp ON sol.product_id  = dp.product_id
                    AND so.order_date BETWEEN dp.valid_from
                                          AND COALESCE(dp.valid_to, '9999-12-31')
-- dim_sales_rep same pattern (when rep_id is NOT NULL)
```

This guarantees historical correctness: an order placed before a
customer's segment changed attributes to the *old* segment, not the
current one.

**Derivation rules per measure (build at Gold from Silver):**

| Measure | Formula | Source |
|---|---|---|
| `quantity_ordered` | pass-through | `silver.order_lines.quantity` |
| `unit_price_at_sale` | pass-through | `silver.order_lines.unit_price` (price-at-sale is captured at OLTP — no need to look up dim_product) |
| `discount_amount` | pass-through | `silver.order_lines.discount_amt` |
| `line_total_inr` | pass-through | `silver.order_lines.line_total` (source-computed: `quantity * unit_price - discount_amt`; post-discount, pre-tax) |
| `tax_amount` | `round(line_total_inr * 0.18, 2)` | derived (India GST 18% on post-discount net) |
| `net_revenue_inr` | `line_total_inr` | pre-tax net revenue — standard analytics measure (gross of discount → minus discount = net pre-tax → `line_total` already represents this) |

**`territory_id` derivation:**

| Channel | territory_id source |
|---|---|
| STORE | `silver.stores.territory_id` where `store_id` matches |
| B2B | `dim_sales_rep.territory_id` (as-of join on `order_date`) |
| D2C | sentinel `'D2C_NATIONAL'` (FK to the `dim_territory` sentinel row) |

**`status_id`** — the order's *current* status at fact-build time
(from `silver.orders.status`). The full transition history lives in
`silver.order_status_log`; a separate fact (e.g.
`fact_order_status_transitions`) could be added later if status-flow
analytics are needed.

```
line_id                 STRING    PK (pass-through from source)
order_id                STRING    NK (for join debugging / lineage)
-- Foreign keys
order_date_id           INT       FK -> dim_date         (= cast(order_date as YYYYMMDD INT))
customer_surrogate_key  BIGINT    FK -> dim_customer     (as-of join on order_date)
product_surrogate_key   BIGINT    FK -> dim_product      (as-of join on order_date)
channel_id              STRING    FK -> dim_sales_channel (= channel_type)
rep_surrogate_key       BIGINT    FK -> dim_sales_rep    NULL for D2C/STORE; as-of join for B2B
store_id                STRING    FK -> dim_store        NULL for D2C/B2B
territory_id            STRING    FK -> dim_territory    (per derivation above)
status_id               STRING    FK -> dim_order_status (= silver.orders.status)
-- Measures
quantity_ordered        INT
unit_price_at_sale      DECIMAL(10,2)
discount_amount         DECIMAL(10,2)
line_total_inr          DECIMAL(12,2)
tax_amount              DECIMAL(10,2)
net_revenue_inr         DECIMAL(12,2)
-- Audit
_pipeline_run_id        STRING
_ingestion_timestamp    TIMESTAMP   -- pulled through from silver.order_lines
_gold_timestamp         TIMESTAMP
```

### gold.fact_daily_channel_revenue

Grain: `(date_id, channel_id, category_id, territory_id)`. Pre-aggregated
for BI dashboards.

**Source:** `gold.fact_order_line` joined to `gold.dim_product` (for
`category_id`), grouped by the four grain columns. A Gold→Gold read is
allowed for strict aggregations like this — keeps the fact-line as the
single source of truth for measure definitions.

```
-- Grain: channel + product_category + date + territory
summary_date_id         INT       FK -> dim_date
channel_id              STRING    FK -> dim_sales_channel
category_id             STRING    FK -> dim_product_category
territory_id            STRING    FK -> dim_territory
-- Measures (all aggregated from fact_order_line over the grain)
total_orders            INT          -- COUNT(DISTINCT order_id)
total_lines             INT          -- COUNT(*)
total_units_sold        INT          -- SUM(quantity_ordered)
gross_revenue_inr       DECIMAL(14,2)  -- SUM(quantity_ordered * unit_price_at_sale)
total_discount_inr      DECIMAL(14,2)  -- SUM(discount_amount)
net_revenue_inr         DECIMAL(14,2)  -- SUM(net_revenue_inr) i.e. SUM(line_total_inr)
total_tax_inr           DECIMAL(14,2)  -- SUM(tax_amount)
avg_order_value_inr     DECIMAL(10,2)  -- net_revenue_inr / total_orders
return_rate_pct         DECIMAL(5,2)   -- 100.0 * COUNT(*) WHERE status='RETURNED' / total_lines
-- Audit
_pipeline_run_id        STRING
_gold_timestamp         TIMESTAMP
```

### gold.fact_inventory_daily

Grain: `(product_id, store_id, snapshot_date)` — directly from
`silver.inventory_snapshot` (already that grain at source).

**`product_surrogate_key` lookup** uses an as-of join on `snapshot_date`
against `dim_product` — the inventory line attributes to whichever
product version was current on that snapshot date.

**`days_of_stock_remaining` derivation:** computed in the Gold notebook
as `closing_stock / NULLIF(avg_daily_units_sold_7d, 0)`, where
`avg_daily_units_sold_7d` is the trailing 7-day mean of `units_sold` for
that `(product_id, store_id)` pair. NULL when the average is zero or
when there's not yet 7 days of history. Cast to INT.

```
-- Grain: product + store + date
snapshot_date_id        INT       FK -> dim_date
product_surrogate_key   BIGINT    FK -> dim_product (as-of join on snapshot_date)
store_id                STRING    FK -> dim_store
-- Measures
opening_stock           INT
units_sold              INT
units_returned          INT
closing_stock           INT
stockout_flag           BOOLEAN
reorder_point           INT
days_of_stock_remaining INT       NULL
-- Audit
_pipeline_run_id        STRING
_ingestion_timestamp    TIMESTAMP    -- pulled through from silver.inventory_snapshot
_gold_timestamp         TIMESTAMP
```

---

## Quarantine tables

Quarantine lives in its **own UC catalog**: `quarantine.default.{entity}`,
one table per Silver entity. The catalog name mirrors the medallion
contract — quarantine is a peer of bronze/silver/gold, not a sub-tree of
silver. External location is `abfss://quarantine@pipelineiqadlsdev/...`.

Every quarantine table follows the same shape — only `source_table`
varies. Tables are created lazily on the first DQ failure for a given
entity (Silver notebooks no-op the write when there are zero rejects).

```
quarantine_id       STRING    PK (auto-generated UUID via uuid())
rejection_reason    STRING    -- `;`-separated list of rejection codes
pipeline_run_id     STRING
rejected_at         TIMESTAMP
raw_record          STRING    -- JSON-serialised original record (key columns + audit)
source_table        STRING    -- e.g. 'velora_oms.orders', 'velora_crm.customers'
```

**Existing quarantine tables** (one per Silver entity, lazily created):
- `quarantine.default.orders`
- `quarantine.default.order_lines`
- `quarantine.default.order_status_log`
- `quarantine.default.customers`
- `quarantine.default.customer_addresses`
- `quarantine.default.products`
- `quarantine.default.product_pricing`
- `quarantine.default.inventory_snapshot`
- `quarantine.default.sales_reps`
- `quarantine.default.territory_assignments`

Quarantine is **append-only** — the same row may be re-quarantined on
subsequent runs (different `quarantine_id`, same `rejection_reason`).
De-dup is a reporting concern, not a write-time concern.

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

### #6 — 2026-05-11 (Session 11.2) — Bronze count + Silver per-table deviations clarified

- **Change:** two consistency fixes after chunk 4 landed:
  - Bronze section now correctly says **12 tables** (was 10) — the static
    reference tables `product_categories` + `stores` are bronze entities
    too per DECISIONS #60, even though they bypass Silver and feed Gold
    static dims directly. Both items added to the bronze table list.
  - Silver "Conventions for all Silver tables" section now lists three
    documented per-table deviations: (a) `silver.inventory_snapshot`
    partitions by `snapshot_date` not `_silver_date` (DECISIONS #63),
    (b) inventory dedups on the composite (product_id, store_id,
    snapshot_date) not the bare PK, (c) `silver.order_status_log` allows
    the 7-state set including `RETURN_INITIATED` (DECISIONS #64).
- **Why:** the Bronze count drift was a real stakeholder-facing
  inaccuracy after S9.5 + S10 + S11.2 added new entities. The Silver
  deviations were correctly captured in DECISIONS but invisible from
  SCHEMA — meaning a fresh reader of the conventions section would think
  the rules were universally applied, then be surprised by the inventory
  notebook code. Documenting deviations next to the convention closes
  that gap.
- **Code impact:** none (notebooks already implement this; only the docs
  were stale).
- **Reference:** DECISIONS #60, #63, #64.

### #5 — 2026-05-07 (Session 8) — Silver + Gold full spec-out, derivation rules locked

- **Change:** end-to-end refinement of the medallion blueprint to remove
  ambiguity before Phase 2 build-out continues. Specifically:
  - **Silver:** all 10 Silver tables now have explicit column lists and
    DQ-rule sets. The `(Remaining Silver tables follow the same pattern…)`
    placeholder is gone. Conventions section added at top of Silver
    documenting audit columns, DQ flags, dedup, MERGE, partition,
    quarantine routing, and SCD-change-tracking pattern.
  - **Gold:** Conventions section added documenting surrogate-key rule
    (`xxhash64(NK, valid_from)`), `valid_from` rule (earliest known
    activity date — required for as-of joins to work on historical
    facts), `valid_to` rule, fact→dim as-of join pattern, and static-dim
    bypass-Silver convention.
  - **`gold.dim_product`:** SCD-2 `list_price` source clarified — comes
    from `silver.product_pricing` (separate table), not
    `silver.products`. `valid_from` on dim row tracks
    `product_pricing.effective_from`, so the SCD timeline matches source
    pricing dates (not Gold processing dates) — critical for as-of
    joins on historical orders. Added `pricing_type` column.
  - **`gold.dim_sales_rep`:** same pattern — SCD-2 `territory_id`
    sourced from `silver.territory_assignments` (event log), not
    `silver.sales_reps`.
  - **`gold.dim_territory`:** synthesis path documented; sentinel row
    `D2C_NATIONAL` added to keep `fact_order_line.territory_id` NOT
    NULL for D2C orders.
  - **`gold.fact_order_line`:** PK is **pass-through `line_id`** (source
    UUID is already unique, no synthesis needed). Explicit derivation
    table for all 6 measures: `tax_amount = round(line_total_inr *
    0.18, 2)` (India GST 18%), `net_revenue_inr = line_total_inr`
    (post-discount, pre-tax). `territory_id` derivation per channel
    (STORE → store, B2B → as-of dim_sales_rep, D2C → `D2C_NATIONAL`).
    SCD-2 fact→dim joins use as-of pattern on `order_date`.
  - **`gold.fact_inventory_daily`:** `days_of_stock_remaining` formula
    spec'd (closing_stock / 7-day rolling avg of units_sold).
  - **`gold.fact_daily_channel_revenue`:** sourced from
    `gold.fact_order_line` + `dim_product` join, grouped by grain.
    Per-measure aggregation rules documented. Added `total_tax_inr`.
  - **Static dims** (`dim_product_category`, `dim_sales_channel`,
    `dim_order_status`, `dim_date`, `dim_store`) gained explicit
    `_pipeline_run_id` + `_gold_timestamp` audit columns; their
    bypass-Silver semantics noted.
  - **Bronze:** `_ingestion_date` partition column made explicit in
    column list (was prose-only).
  - **Quarantine:** moved to its own UC catalog (`quarantine.default.*`)
    per medallion-peer principle. All 10 entities listed; convention
    that tables are lazily created on first DQ failure documented.
- **Why:** Phase 2's first vertical slice (silver.orders, silver.customers,
  gold.dim_customer) was built in S8 and exposed gaps in the spec —
  several "follow the same pattern" placeholders, missing derivation
  formulas, ambiguous source-of-truth for SCD-2 attrs (e.g.
  `dim_product.list_price` was specced as if it lived on
  `silver.products`, but the source has it on a separate event log).
  Continuing into `fact_order_line` without a tightened blueprint
  guaranteed bespoke decisions per notebook + drift between code and
  spec. This pass refits the schema as a single coherent contract that
  all remaining notebooks code against.
- **Code impact:** `silver.orders` and `silver.customers` align with
  the refined spec — no rework. **`gold.dim_customer` needs one fix +
  rebuild:** the S8 build hardcoded `valid_from = current_date()` (no
  rule existed yet); the new spec mandates earliest known activity date.
  As shipped, every `fact_order_line` as-of join would miss
  `dim_customer` because all 3,619 existing orders are dated before
  the dim's `valid_from = 2026-05-07`. Fix is a 5-line change in
  `notebooks/gold/build_gold_dim_customer.py` to look up
  `MIN(silver.orders.order_date)` per customer; then drop + re-run the
  notebook (idempotent surrogate keys make the rebuild cheap). Going
  forward, every Silver/Gold notebook picks columns + DQ rules +
  derivation formulas straight from this file with no design ambiguity.
- **Reference:** PROGRESS.md S8 session log (to be appended);
  DECISIONS.md will gain new rows for the high-impact design calls
  (4-question consult: silver.products+pricing two-table model,
  fact→dim as-of join, valid_from = earliest activity, fact PK
  pass-through; plus tax/territory derivations).

### #4 — 2026-05-01 (Session 5) — `## Data lifecycle, frequency, volume` section added at top of SCHEMA.md

- **Change:** new top-level section before the per-table specs covering
  generator cadence (manual cmd-line, no automation), four data-evolution
  patterns (append-only events, mutable dimensions, daily snapshot, static
  reference), per-day / 30-day / 90-day / 1-year volume forecast, and
  real-world retail scale comparison (Velora vs. Croma vs. Walmart).
- **Why:** during Session 5 wrap, the user surfaced a recurring confusion
  about whether the source runs daily, what `inventory_snapshot` actually
  captures (snapshot vs. CDC), and whether the volume scales — answered
  inline, then asked for it durably documented. The section answers four
  distinct future-author questions in one place: (1) "is the source
  generating new rows right now?" (no), (2) "what gets UPDATEd in place
  vs. appended?" (table-by-table), (3) "how big does this get over time?"
  (concrete projections), (4) "is this realistic?" (yes — under-provisioned
  vs. real retail, see comparison table).
- **Generator impact:** none. The section is descriptive of existing
  behavior; no code changes.
- **Reference:** the conversation in the Session 5 transcript.

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
