# notebooks/gold/

Gold layer Databricks notebooks.

## Phase

Phase 2 — Medallion Pipeline

## Responsibility

Build the warehouse-shaped star schema from Silver Delta tables.
Apply SCD logic, assign surrogate keys, pre-aggregate facts.

## What this layer builds

### Dimensions (9 tables)
- dim_customer — SCD Type 2 on segment and city
- dim_product — SCD Type 2 on list_price
- dim_product_category — SCD Type 0 (static)
- dim_sales_channel — SCD Type 0 (static)
- dim_sales_rep — SCD Type 2 on territory
- dim_territory — SCD Type 1
- dim_order_status — SCD Type 0 (static)
- dim_date — SCD Type 0, pre-built for 2020-2030
- dim_store — SCD Type 1

### Facts (3 tables)
- fact_order_line — Grain: one SKU on one order
- fact_daily_channel_revenue — Grain: channel + category + date (pre-aggregated)
- fact_inventory_daily — Grain: product + store + date

## SCD rules
- Type 2: new row per change, valid_from/valid_to/is_current columns
- Type 1: UPDATE in place, no history
- Type 0: never updated

## External tables
All Gold tables are registered as External Tables in Unity Catalog,
pointing to ADLS Delta paths. Queryable from Databricks SQL Warehouse
via JDBC/ODBC (VS Code, Power BI).

## Populated

End of Phase 2.
