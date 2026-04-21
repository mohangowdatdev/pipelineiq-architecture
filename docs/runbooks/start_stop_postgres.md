# Runbook: Start and Stop PostgreSQL Flexible Server

*Populated at end of Phase 0.*

---

## Purpose

PostgreSQL Flexible Server (B2s tier) costs approximately Rs. 950/month when running
continuously. Stopping it nights and weekends saves ~Rs. 300/month. This runbook
documents how to stop and start the server safely.

The server stores the PipelineIQ control plane: watermarks, entity registry, file
registry, pipeline execution log, incident store, and IaC embeddings. Stopping it
means no pipeline control plane operations can run. The generator (Azure SQL) is
unaffected by PostgreSQL being stopped.

---

## Contents (to be written at end of Phase 0 once PostgreSQL is provisioned)

- How to stop the server (Azure CLI command)
- How to start the server (Azure CLI command)
- How to verify it is running and accepting connections
- What to do if the server is stopped but a pipeline run is in progress
- Automation: scheduled start/stop via Azure Automation or Azure CLI cron
- How stopping affects ADF pipeline runs (what fails, what retries safely)
- Connection string format and where it is stored in Key Vault
