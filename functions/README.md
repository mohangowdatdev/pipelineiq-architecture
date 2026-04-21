# functions/

Azure Functions — control plane API.

## Phase

Phase 2 — Medallion Pipeline (watermark and file registry functions)
Phase 5 — AI RCA Backend (failure event detection functions)

## Responsibility

HTTP-triggered Python Azure Functions that wrap the PostgreSQL control plane
as a REST API. ADF calls these functions during pipeline execution to:

- Read watermarks before extraction
- Register landed files after copy
- Commit watermarks after successful pipeline completion
- Queue pipeline execution events
- Log pipeline run status

## Functions to be created

| Function | Trigger | Called by | Purpose |
|---|---|---|---|
| get_watermark | HTTP | ADF | Returns last_successful_load for an entity |
| commit_watermark | HTTP | ADF | Advances watermark after confirmed success |
| register_file | HTTP | ADF | Records a landed file in file_registry |
| get_queue | HTTP | ADF | Returns next entity to process |
| log_pipeline_run | HTTP | ADF / Databricks | Appends to pipeline_exec_log |

## Important

The watermark commit function advances the watermark ONLY when the pipeline
reports confirmed success. On failure, the watermark stays at the previous
value so the next run retries the same window.

## Populated

End of Phase 2.
