# notebooks/bronze/

Bronze layer Databricks notebooks.

## Phase

Phase 2 — Medallion Pipeline

## Responsibility

Read raw Parquet files from ADLS `landing/` and write schema-enforced,
audit-augmented, append-only Delta tables to ADLS `bronze/`.

No business logic. No deduplication. No schema transformation.

## What gets added at this layer

Every Bronze table adds these four audit columns:

| Column | Type | Value |
|---|---|---|
| _source_file | STRING | Full ADLS path of the source Parquet file |
| _ingestion_timestamp | TIMESTAMP | When ADF wrote the file to landing |
| _pipeline_run_id | STRING | ADF pipeline run ID (GUID) |
| _bronze_timestamp | TIMESTAMP | When this Databricks job wrote the row to bronze |

## Partitioning

All Bronze tables partition by `_ingestion_timestamp::DATE`.
This means each day's run creates one new partition per table.

## Tables written

bronze.orders, bronze.order_lines, bronze.order_status_log,
bronze.customers, bronze.customer_addresses, bronze.products,
bronze.product_pricing, bronze.inventory_snapshot,
bronze.sales_reps, bronze.territory_assignments

## Populated

End of Phase 2.
