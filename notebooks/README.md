# notebooks/

Databricks PySpark notebooks that implement the medallion ETL pipeline.

Each subfolder corresponds to one layer of the medallion architecture.
Notebooks are deployed as Databricks Jobs (not all-purpose cluster runs).

## Phase

Phase 2 — Medallion Pipeline

## Structure

| Folder | Layer | Responsibility |
|---|---|---|
| bronze/ | Bronze | Schema enforcement, audit columns, append-only Delta write |
| silver/ | Silver | Deduplication, DQ validation, MERGE on business key, quarantine routing |
| gold/ | Gold | SCD logic, surrogate key assignment, star schema build, pre-aggregations |

## Medallion boundaries (ENFORCED)

- Data flows forward only: landing → bronze → silver → gold
- No notebook reads from a downstream layer
- No notebook writes to an upstream layer
- Gold notebooks read from silver only

## Databricks runtime

All notebooks target Databricks Runtime 14.x LTS.

Spark config required on every cluster:
```
spark.sql.extensions io.delta.sql.DeltaSparkSessionExtension
spark.sql.catalog.spark_catalog org.apache.spark.sql.delta.catalog.DeltaCatalog
```

## Cluster config

Jobs Compute (ETL runs):
- Node type: Standard_DS3_v2
- Min workers: 1, Max workers: 2
- Auto-terminate: 30 minutes

All-purpose (development only):
- Single node, auto-terminate 30 minutes
- NEVER leave running
