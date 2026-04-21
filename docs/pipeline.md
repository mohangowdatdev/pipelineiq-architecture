# PipelineIQ — ADF Pipeline and Medallion ETL

*Populated at end of Phase 2.*

---

## Summary

This document covers the full ADF pipeline design and Databricks ETL notebooks
for the PipelineIQ medallion pipeline. It describes the ADF parameterised pipeline
structure, the watermark-based incremental extraction strategy, the Azure Functions
control plane, and each Databricks notebook's logic, schema transformations, and
DQ rules.

It will be written once Phase 2 pipeline build is complete and a full pipeline run
has been verified end-to-end. This document is the reference for anyone who needs
to understand how data moves from Azure SQL to the Gold Delta tables.

---

## Contents (to be written at end of Phase 2)

- ADF pipeline JSON structure and parameterisation
- How entity_registry drives pipeline execution (config-driven design)
- Watermark strategy: how watermarks are read, used, and committed
- Per-entity extraction queries (the SELECT WHERE updated_at > watermark pattern)
- Landing zone file naming convention
- Bronze notebook: schema enforcement, audit column addition, Delta write
- Silver notebook: MERGE logic, DQ rules per entity, quarantine routing
- Gold notebooks: SCD Type 2 logic, surrogate key assignment, fact table build
- Quarantine table schema and rejection reason codes
- How to re-process a failed run (watermark stays, next run retries window)
- Databricks job configuration and scheduling
- Unity Catalog table registration
