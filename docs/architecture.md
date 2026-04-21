# PipelineIQ — System Architecture

*Populated at end of Phase 0.*

---

## Summary

This document covers the full system topology of PipelineIQ as deployed on Azure.
It describes every component, its role in the architecture, the data flows between
components, network boundaries, identity and access patterns, and the cost structure.

It will be written once Phase 0 Terraform provisioning is complete and all Azure
resources exist and have been verified. At that point, this document describes what
was actually built — not what was planned.

---

## Contents (to be written at end of Phase 0)

- System topology diagram (text-based)
- All Azure resources and their tiers
- Networking: VNets, private endpoints, public endpoints
- Identity: managed identities, Key Vault access policies, RBAC assignments
- Data flows: Azure SQL → ADF → ADLS → Databricks → SQL Warehouse
- Control flows: ADF → Azure Functions → PostgreSQL
- Observability flows: Azure Monitor → Log Analytics → FastAPI → PostgreSQL
- AI flows: pgvector retrieval → GPT-4o → incident_store → Slack
- Terraform module structure and what each module provisions
- Unity Catalog setup: metastore, catalog, schemas
- Cost breakdown by component
