---
name: PipelineIQ project state
description: Current phase, what exists, and where to find the canonical session record
type: project
---

Session 1 complete (2026-04-20). Generator fully built. Phase 0 Terraform is next.

For the full record of what was built and why, read **PROGRESS.md → ## Session Log → 2026-04-20**.
For all architectural decisions, read **DECISIONS.md** (entries #1-24).

Non-obvious things not derivable from the code:
- Generator is unverified against a live database — Phase 0 must complete first.
- `inventory_snapshot` has no UNIQUE constraint on (product_id, store_id, snapshot_date). Intentional — duplicates are deduplicated in Silver. Note this when writing the Silver notebook.
- The `control_flags` table in `velora_oms` is not in SCHEMA.md — added this session for the dependency_violation failure scenario. Not extracted by ADF.
- Catalogue seed on first run takes ~60s (4,200 product inserts). Expected. Within the 10-min Azure Function timeout.

**Why:** State is correct per SCHEMA.md. Phase 0 provisions the Azure resources the generator writes to.
**How to apply:** Do not build Phase 2 notebooks or Phase 5 FastAPI until Phase 0 Azure SQL exists and generator is verified end-to-end.
