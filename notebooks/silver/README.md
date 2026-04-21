# notebooks/silver/

Silver layer Databricks notebooks.

## Phase

Phase 2 — Medallion Pipeline

## Responsibility

Read from Bronze Delta tables. Deduplicate, validate data quality,
MERGE on business key, route bad records to quarantine.

## What this layer enforces

1. **Deduplication** — MERGE ON business key. No duplicate order_ids,
   customer_ids, etc. can exist in Silver.

2. **DQ validation** — Rules applied per entity:
   - Not-null checks on required fields
   - Referential integrity checks (order_id must exist for order_lines)
   - Range checks (unit_price must be > 0)
   - Bad records written to quarantine with rejection_reason

3. **Channel unification** — D2C, B2B, and Store orders are unified
   under a single `channel_type` column in silver.orders.

4. **SCD change tracking** — silver.customers carries `_scd_changed`,
   `_prev_segment`, `_prev_city` so Gold knows when to open a new SCD row.

## Quarantine routing

Records that fail DQ go to quarantine.orders or quarantine.order_lines.
They are never deleted. They contain rejection_reason + pipeline_run_id.

## Populated

End of Phase 2.
