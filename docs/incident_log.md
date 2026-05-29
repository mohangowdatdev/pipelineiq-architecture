# PipelineIQ — Incident & Blocker Log

Topic-organised journal of every non-trivial blocker, bug, or
production-style incident encountered during the build. Unlike
PROGRESS.md's `Broke` field (chronological, session-scoped), this log
is **append-only, topic-organised, and indexed** so future-you can
answer questions like "have we hit this before?" and "what was the
hardest part of the build?" without paging through 10 session logs.

Add a new entry the moment an incident crosses the "spent more than
15 minutes on it" threshold. Resolve the entry the moment the fix lands.
Never edit prior entries — superseded findings get a `superseded by #N`
note inline.

---

## Entry schema

```
### #N — YYYY-MM-DD — Short title
- **Phase / Session:** Phase X, Session N
- **Category:** infra | network | auth | code | data | perf | ops | tooling
- **Severity:** blocker | major | minor
- **Effort:** rough wall-clock from symptom to fix (diagnose + patch)
- **Status:** resolved YYYY-MM-DD | ongoing | deferred
- **Symptom:** the observable — error string, behaviour, what failed
- **Root cause:** what was actually wrong
- **Fix:** what made it go away
- **Prevention / first-check:** how to avoid next time, or the one-line
  check to run before chasing speculative causes
- **References:** DECISIONS #N, commit SHA, PROGRESS session date
```

---

## Index

High-to-low severity, most recent first within severity. The `Hardest`
column is a subjective call — "yes" means the incident taught something
non-obvious or took disproportionate time relative to the fix.

| # | Date | Title | Severity | Category | Effort | Hardest? |
|---|------|-------|----------|----------|--------|----------|
| 24 | 2026-05-29 | Stale `.env` (wrong SQL hostname) misread as DB cold-start — `HYT00 Login timeout` retry loop swallowed the real error | major | ops | ~90 min | **yes** |
| 23 | 2026-05-25 | `jobs.submit` SubmitTask doesn't accept `job_cluster_key` — needs persistent Job | minor | tooling | ~10 min | no |
| 22 | 2026-05-25 | SQL Server `SUM(bit)` rejects in Spark JDBC schema probe | minor | code | ~5 min | no |
| 21 | 2026-05-25 | Databricks KV-backed secret scope: AzureDatabricks first-party SP missing KV `Secrets User` role | major | auth | ~15 min | no |
| 20 | 2026-05-14 | `dim_customer` SCD-2 surrogate-key collisions — Spark lazy-eval across write boundary | major | data | ~4 hrs across S12 (misdiag) + S13 (real fix) | **yes** |
| 19 | 2026-05-11 → 2026-05-25 | **Flex Function App worker reaper kills pyodbc inventory write — 11 days of silent partial fires** | blocker | infra | ~10 hrs across S11.1 + S12 + S13 + S14 | **yes** |
| 18 | 2026-05-09 | Flex Consumption timer trigger ALSO silently no-ops (DECISIONS #50 didn't fully solve it) | major | infra | ~30 min | **yes** |
| 17 | 2026-05-06 | Daily Function timer fired exactly once on Y1 Linux Consumption | major | infra | ~90 min (diag + Flex migration + re-grant + redeploy) | **yes** |
| 16 | 2026-05-01 | Cluster-policy autotermination invalid for job clusters | minor | infra | ~5 min | no |
| 15 | 2026-05-01 | `partitionBy()` rejects Column expressions, requires names | minor | code | ~3 min | no |
| 14 | 2026-05-01 | UC metastore creation hit "1 per region per account" limit | major | infra | ~15 min | **yes** |
| 13 | 2026-04-22 | Stores missing from `seed_to_db` — silent orphan data | major | data | ~20 min | no |
| 12 | 2026-04-22 | Day 21 `schema_drift` failure injection was a no-op at Azure SQL | major | data | ~30 min diag, architectural fix | **yes** |
| 11 | 2026-04-22 | `scripts/backfill_inventory.py` v1 was 15× too slow | minor | perf | ~25 min | no |
| 10 | 2026-04-22 | Inventory snapshot single-transaction TCP reset | blocker | network | ~90 min | **yes** |
| 9  | 2026-04-22 | Generator RNG seed constant across dates → PK collision on day 2+ | blocker | code | ~40 min | **yes** |
| 8  | 2026-04-22 | Azure SQL serverless cold start exceeds pyodbc 30 s timeout | blocker | infra | ~15 min | no |
| 7  | 2026-04-21 | Generator cursor-vs-conn + missing `fast_executemany` | blocker | code | ~45 min | no |
| 6  | 2026-04-21 | Portal AI returning HTTP 401 — env-var key didn't match any live key | blocker | auth | ~3 hr | **yes** |
| 5  | 2026-04-21 | Bootstrap SQL blocked — laptop IP not in either server's firewall | blocker | network | ~45 min, session-crossing | no |
| 4  | 2026-04-21 | `azure.extensions` Postgres server parameter is case-sensitive | major | infra | ~10 min | no |
| 3  | 2026-04-21 | `grant_current_user_*` RBAC failed — Contributor can't write role assignments | blocker | auth | ~15 min + propagation | no |
| 2  | 2026-04-21 | Terraform no longer in Homebrew core | minor | tooling | ~5 min | no |
| 1  | 2026-04-21 | `msodbcsql18` brew install deadlocked on EULA | blocker | tooling | 55 min lost, session-crossing | no |

---

## Entries

### #24 — 2026-05-29 — Stale `.env` misread as DB cold-start
- **Phase / Session:** Phase 2 / Session 16
- **Category:** ops
- **Severity:** major
- **Effort:** ~90 min (4 wrong hypotheses chased before checking env vars)
- **Status:** resolved 2026-05-29
- **Symptom:** `scripts/audit_fires.py` against `velora_oms` failed three times in a row with the same trace: 12 cold-start retry attempts, each printing `[attempt N] cold-start: sleeping Xs …`, then `RuntimeError: Failed to wake Azure SQL after 12 attempts`. Direct `python -c "config.connect_aad(timeout=60)"` returned `OperationalError: ('HYT00', '[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired (0) (SQLDriverConnect)')`. Even a 300-second timeout connect failed with the same HYT00. Azure portal said the DB was paused (last paused 03:05 UTC).
- **Wrong hypotheses chased (in order):**
  1. *Firewall stale* — ran `scripts/update_sql_firewall_ip.sh`, "already points at 223.185.131.196 — nothing to do." Cross-checked rules table: our IP present, VPN IP present, Azure-services rule present. Not firewall.
  2. *DB stuck paused* — bumped `auto-pause-delay 60 → 120` via `az sql db update`. Status flipped Paused → Online with a real `resumedDate`. Connect still HYT00.
  3. *Token refresh / AAD cache* — verified `az account show` was on Sponsorship sub. Reissued `az login`. Same HYT00.
  4. *Network path* — ruled out by the fact that `az sql db show` (management plane, same hostname) responded fast. Only the data plane was dead.
- **Root cause:** `.env` was stale from S14 — `AZURE_SQL_SERVER=pipelineiq-sql-dev.database.windows.net` (server renamed away in S6 to `pipelineiq-sql-velora-dev`) and `AZURE_SQL_DATABASE=velora` (correct name is `velora_oms`). Every connect attempt resolved DNS to a non-existent hostname and timed out. The audit script's `connect_with_retry` catches `40613` / `HYT00` / `is not currently available` and silently retries — same UX for "DB is paused, wake in progress" vs "hostname doesn't exist." The wake-state hypothesis was reinforced by the genuine paused-DB state (true but coincidentally true).
- **Fix:** Two lines in `.env`: `AZURE_SQL_SERVER=pipelineiq-sql-velora-dev.database.windows.net` + `AZURE_SQL_DATABASE=velora_oms`. Re-ran the audit — succeeded in ~10s with full 7-day output.
- **Prevention / first-check:** When `audit_fires.py` (or any retry-wrapped connection script) hits its retry budget, INSTRUMENT the actual `pyodbc` error string before chasing new hypotheses. Two minutes of `print(str(e))` would have surfaced the wrong-hostname state on attempt 1. The blanket `HYT00 → retry` pattern is a symptom-level catch — for a tool that should diagnose, error strings need to be preserved + visible. Secondary lesson: every session-start should grep `.env` against the current resource names — the S14 carry-over had been documented as a follow-up and never executed; documentation alone wasn't enough.
- **References:** PROGRESS.md 2026-05-29 (S16 session log → "Broke" → "First 90 minutes burned chasing the wrong root cause"). Commit `fc9b104`.

---

### #23 — 2026-05-25 — `jobs.submit` SubmitTask doesn't accept `job_cluster_key`
- **Phase / Session:** Phase 2 catch-up, Session 15
- **Category:** tooling
- **Severity:** minor
- **Effort:** ~10 min
- **Status:** resolved 2026-05-25
- **Symptom:** First run of `scripts/catchup_medallion.py --layer bronze` errored with `TypeError: SubmitTask.__init__() got an unexpected keyword argument 'job_cluster_key'`. The intent was to share one cluster across 10 parallel bronze tasks via `job_clusters` + `job_cluster_key`.
- **Root cause:** The Databricks Python SDK splits Jobs API into two paths: `jobs.submit` (one-time runs, uses `SubmitTask`) and `jobs.create` + `jobs.run_now` (persistent jobs, uses `Task`). Only the persistent-Job path accepts `job_cluster_key`. `SubmitTask` requires each task to define its own `new_cluster` (cluster-per-task) or reference an `existing_cluster_id`.
- **Fix:** Refactored `catchup_medallion.py` to use `jobs.create` (with `job_clusters` array) + `jobs.run_now` to trigger. After the run completes, `jobs.delete(job_id)` cleans up the workspace. Same cost as the original intent (one shared cluster); just a different API path.
- **Prevention / first-check:** When building multi-task Jobs with shared cluster compute, use `jobs.create` (persistent) + `run_now`, not `jobs.submit` (one-time). The SDK's `SubmitTask` is for the lighter "fire one notebook on one cluster" pattern.
- **References:** `scripts/catchup_medallion.py`, PROGRESS S15.

### #22 — 2026-05-25 — SQL Server `SUM(bit)` rejects in Spark JDBC schema probe
- **Phase / Session:** S14, inventory notebook smoke test
- **Category:** code
- **Severity:** minor
- **Effort:** ~5 min
- **Status:** resolved 2026-05-25
- **Symptom:** Smoke test of `notebooks/source_sim/write_inventory_snapshot.py` errored at the final verify step: `com.microsoft.sqlserver.jdbc.SQLServerException: Operand data type bit is invalid for sum operator.` The actual JDBC write had succeeded (189,225 rows landed), but the verify SELECT failed.
- **Root cause:** The verify query had `SUM(stockout_flag)`. `stockout_flag` is `BIT` in SQL Server's `velora_pim.inventory_snapshot` schema. SQL Server rejects `SUM` on `BIT`. Spark's `spark.read.format("jdbc").option("query", ...).load()` triggers a schema probe (`SELECT * FROM (<query>) WHERE 1=0`) which propagates SQL Server's rejection up.
- **Fix:** `SUM(stockout_flag)` → `SUM(CAST(stockout_flag AS INT))`. One-line patch.
- **Prevention / first-check:** SQL Server has stricter type rules than ANSI SQL — `BIT` can't be aggregated except with `MIN`/`MAX`. When writing portable SQL that touches Spark JDBC + SQL Server, cast BIT/BOOLEAN to INT before aggregating.
- **References:** `notebooks/source_sim/write_inventory_snapshot.py`, PROGRESS S14.

### #21 — 2026-05-25 — Databricks KV-backed secret scope: AzureDatabricks SP missing KV `Secrets User` role
- **Phase / Session:** S14, inventory notebook smoke test
- **Category:** auth
- **Severity:** major
- **Effort:** ~15 min (diag + IaC + apply)
- **Status:** resolved 2026-05-25
- **Symptom:** First smoke test of the inventory notebook errored with `PERMISSION_DENIED: Invalid permissions on the specified KeyVault https://pipelineiq-kv-dev.vault.azure.net/. Caller: AzureDatabricks (oid=ee589af4-a29c-4ed9-9108-b64d579f4f42); Action: Microsoft.KeyVault/vaults/secrets/getSecret/action; Assignment: (not found)`. The notebook was trying to read `sql-admin-password` from KV via `dbutils.secrets.get(scope="pipelineiq-dev-kv", key=...)`.
- **Root cause:** KV-backed secret scopes (Databricks `databricks_secret_scope` with `keyvault_metadata`) call Key Vault on the workspace's behalf via the **AzureDatabricks first-party Service Principal** (well-known app id `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`; tenant-scoped object_id `ee589af4-a29c-4ed9-9108-b64d579f4f42` in our tenant) — NOT via the workspace access connector MSI. The SP had no role on the vault because no prior notebook had ever read a secret (bronze/silver/gold read from Delta Lake, not KV). The KV is in RBAC mode (not access-policy), so the SP needed `Key Vault Secrets User` role.
- **Fix:** Added `azurerm_role_assignment.databricks_kv_secrets_user` to `core/databricks_uc/main.tf` granting `Key Vault Secrets User` to `var.azure_databricks_sp_object_id`. New var wired through to `clients/velora/terraform.tfvars` with the tenant-scoped object_id. `terraform apply` completed in 27s. Re-ran smoke test: clean. DECISIONS #71.
- **Prevention / first-check:** When introducing the FIRST notebook that uses a KV-backed secret scope, also grant the AzureDatabricks first-party SP `Key Vault Secrets User` on the vault. Get the object_id via `az ad sp show --id 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d --query id -o tsv`. This is NOT documented prominently in Databricks docs; the error message at least names the SP clearly so you can grep for it.
- **References:** DECISIONS #71, `PipelineIQ-IaC/core/databricks_uc/main.tf`, S14 PROGRESS log.

### #20 — 2026-05-14 — `dim_customer` SCD-2 surrogate-key collisions from Spark lazy-eval
- **Phase / Session:** S12 (mis-diagnosis) → S13 (real fix)
- **Category:** data
- **Severity:** major
- **Effort:** ~4 hrs total across two sessions
- **Status:** resolved 2026-05-14 (S13, DECISIONS #68)
- **Symptom:** S12 health check on `gold.default.dim_customer` found 51/407 rows with duplicate `surrogate_key`. Surrogate key formula is `xxhash64(customer_id, valid_from)`. Both the closed-out OLD version and the inserted NEW version of each CHANGED customer shared the same `valid_from`, producing identical sk. Fact joins via `xxhash64(NK, valid_from)` would attribute facts to the wrong dim version for those 51 customers.
- **Root cause:** Initially (DECISIONS #67) believed to be a formula bug — that the NEW-version rows for SCD-2 CHANGE customers reused the OLD `valid_from` instead of the change-detection date. Closer inspection in S13: the formula in `build_gold_dim_customer.py` was CORRECT (`F.when(_action == "NEW", first_activity_date).otherwise(current_date())`). Real root cause was **Spark lazy DataFrame re-evaluation across a write boundary.** `df_actioned` (lazy) was computed in step 3, then step 4's MERGE flipped `is_current=false` on SCD2_CHANGE rows. When step 6 evaluated `df_actioned.filter(_action.isin("NEW","SCD2_CHANGE"))`, Spark re-evaluated upstream `df_dim_current` — which now no longer found the just-closed customers, so the join produced NULLs, so `_action` flipped from `SCD2_CHANGE` to `NEW`, so `valid_from` defaulted to `first_activity_date` instead of `current_date()`. Identical sk to the closed row.
- **Fix:** First attempt was `.cache()` on `df_actioned` — FAILED. Re-running on fresh data produced 13 NEW collisions. `.cache()` is a hint, not a contract: Spark may evict cached partitions under memory pressure or skip caching for `groupBy.count` shapes. **Real fix** (DECISIONS #68): bulletproof Delta temp-table materialization — `df_actioned.write.format("delta").saveAsTable(_tmp)` + `df_actioned = spark.table(_tmp)` immediately after step 3. Temp table dropped at end of step 8. Pinned to physical storage, immune to Spark's caching decisions. Verified on S15 catch-up wave: 723/723/0 collisions. Bug does NOT affect `dim_product` or `dim_sales_rep` (they derive `valid_from` from immutable source-effective dates, not from a join against the dim's own current state).
- **Prevention / first-check:** **When a Spark notebook does (1) lazy DataFrame computation, (2) a write that mutates a table that DataFrame references, and (3) a later read of the same DataFrame — persist to a Delta temp table, don't rely on `.cache()`.** This pattern is common in SCD-2 close-then-insert flows. The SCD-2 health-check query that surfaces collisions is: `SELECT COUNT(*) FROM (SELECT surrogate_key, COUNT(*) c FROM gold.{dim} GROUP BY surrogate_key HAVING c>1)` — must return 0. Run it after every SCD-2 notebook deploy + after every multi-day catch-up wave.
- **References:** DECISIONS #67 (misdiagnosis), DECISIONS #68 (real fix), `notebooks/gold/build_gold_dim_customer.py`, S12/S13 PROGRESS logs.

### #19 — 2026-05-11 → 2026-05-25 — **Flex Function App worker reaper kills pyodbc inventory write — 11 days of silent partial fires**
- **Phase / Session:** S11.1 → S11.2 → S12 → S13 → S14 (architectural migration)
- **Category:** infra
- **Severity:** blocker
- **Effort:** ~10 hrs across 4 sessions (3 mitigation attempts + final migration)
- **Status:** resolved 2026-05-25 (S14, DECISIONS #71 — supersedes #62 + #69)
- **Symptom:** Daily Function fire at 00:30 UTC. Main batch (orders/lines/status_log/dim_changes) commits cleanly in ~1 min. Then `_write_inventory_snapshot` runs, writes 1-4 chunks of 5K rows each, then silently dies. Worker exits with no exception, no signal back to Python. AppTraces shows the last successful sub-batch trace then silence; App Insights shows no exception. Every fire from 2026-05-14 to 2026-05-24 lost inventory at 1-4 chunks (5K, 10K, 15K, 20K rows out of 189,225) — discovered by `scripts/audit_fires.py` at S14 start.
- **Root cause:** Flex Consumption host runs Python in a separate worker process, communicating via gRPC. Worker reaper kills the worker when it shows no host-visible activity for >~30s, regardless of CPU usage. pyodbc's `cursor.executemany()` blocks the Python thread in C extension code with no callbacks; with 1000-row sub-batches at ~10s each, gRPC keepalive between host and worker silently expires. Plus: each 1000-row sub-batch takes ~10s on Flex's 2GB worker → full 189K rows needs ~31 min, OVER the 30-min function timeout. **The Function App is structurally unfit for 189K-row pyodbc writes regardless of any tuning.**
- **Fix (history of attempts):**
  - **S11.1 (DECISIONS #62):** chunked inventory write 1× → N chunks with per-chunk commit + progress logging. **Partial fix** — durable partial-commit, visible failure point, but didn't address worker-kill.
  - **S13 (DECISIONS #69):** 3-pronged structural fix — (a) timer trigger disabled (Logic App sole fire path, no race), (b) `chunk_size` 10K → 5K (more frequent commits), (c) per-sub-batch `logger.info` for gRPC keepalive. **FAILED in the wild** — 11 consecutive fires all partial.
  - **S14 (DECISIONS #71):** migrated the inventory write OUT of the Function App entirely. New `notebooks/source_sim/write_inventory_snapshot.py` Spark notebook + `core/inventory_workflow/` IaC module (Databricks scheduled Job at 00:35 UTC daily). Spark JDBC bulk insert with numPartitions=8 + batchsize=10000. No Flex reaper, no 30-min function timeout. ~3-4 min wall vs theoretical 30+ min. **Confirmed green** by smoke test on 2026-05-14: 189,225 rows / 4,205 distinct products / 45 distinct stores.
- **Prevention / first-check:** **Function App on Flex Consumption (2GB worker) is unfit for synchronous bulk writes that exceed ~10K rows or ~30s wall time per executable batch.** If a workload looks like "daily snapshot of 100K+ rows via JDBC/pyodbc/executemany," move it to Databricks (Spark JDBC, no host reaper, designed for bulk) or upgrade to EP1 Premium (always-on instance, no idle reaper, +~Rs.8K/mo). EP1 is faster to implement but more expensive; Databricks is the architecturally correct answer when the workload is already medallion-shaped. Telemetry signal: AppTraces shows "last successful sub-batch" then silence (no exception); App Insights memory peaks under 1GB then drops to 0; Function App reports the run as a success (return value present) even though work was incomplete.
- **References:** DECISIONS #62, #66, #69, #71. `generator/main.py::_write_inventory_snapshot`, `notebooks/source_sim/write_inventory_snapshot.py`, `core/inventory_workflow/`. S11.1, S12, S13, S14 PROGRESS logs.

### #18 — 2026-05-09 — Flex Consumption timer trigger ALSO silently no-ops
- **Phase / Session:** S9
- **Category:** infra
- **Severity:** major
- **Effort:** ~30 min (diag + Logic App provisioning)
- **Status:** resolved 2026-05-09 (DECISIONS #59)
- **Symptom:** 2 consecutive scheduled timer fires on the Flex Consumption Function App (May 7 + May 8 windows that should have written 2026-05-07 + 2026-05-08 data) **silently no-op'd**. App Insights showed 0 host-startup traces for those windows — host wasn't waking at all. Function App was healthy on the management plane: `state: Running`, function `enabled: true`, schedule `0 30 0 * * *` unchanged. Manual HTTP invoke via `/admin/functions/generator` worked instantly, proving function code was fine.
- **Root cause:** DECISIONS #50 thought the Y1 → Flex migration fixed timer-from-zero. **It didn't.** Flex's `runOnStartup` semantics only fire on fresh deploys or host wakes; for a Function App that's been idle (no HTTP traffic) for hours, the timer trigger gets the same scale-controller-misses-it problem as Y1 Linux Consumption. The Y1 → Flex migration only fixed manual HTTP invocation reliability, not scheduled-from-zero. We thought Flex was the fix because we kept manually invoking after deploys, which masked the from-zero failure.
- **Fix:** Provisioned `pipelineiq-scheduler-dev` Logic App (Consumption tier, recurrence trigger at 00:30 UTC daily). Logic App POSTs to `/admin/functions/generator` with `x-functions-key` from `azurerm_function_app_host_keys.primary_key` data source. Logic App is Microsoft's managed cron with 99.9% SLA, no scale-to-zero concern, free at our cadence (1 fire/day << 4,000-action free grant). `core/scheduler/` IaC module. DECISIONS #59. Function timer trigger left in place as no-op fallback (idempotency guard makes a double-fire safe). Later in S13 (DECISIONS #69) the timer trigger was disabled entirely (schedule set to Feb 31 = never fires) to eliminate a timer/Logic-App race that contributed to incident #19.
- **Prevention / first-check:** **For ANY daily Function App fire, use Logic App Consumption recurrence trigger as the source of truth, not the Function's built-in timer.** Cost is effectively zero at any reasonable cadence. The Function's timer trigger should only be used when (a) the function is HTTP-hot (frequent invocations keep the host warm) or (b) the cadence is >1/hr (frequent enough that scale-to-zero doesn't kick in). For < hourly cadences, Logic App + admin-endpoint POST is the right pattern.
- **References:** DECISIONS #50 (supersedes the assumption that Flex fixed timer-from-zero), DECISIONS #59 (Logic App provisioning), DECISIONS #69 (timer disabled), `core/scheduler/`, S9 PROGRESS log.

### #17 — 2026-05-06 — Daily Function timer fired exactly once on Y1 Linux Consumption
- **Phase / Session:** Phase 0 carry-over surfaced in S6
- **Category:** infra
- **Severity:** major
- **Effort:** ~90 min total — ~20 min diagnosis, ~30 min Flex IaC + apply + re-grant + redeploy, ~10 min verification, ~30 min real-date generator refactor + backfill (interleaved)
- **Status:** resolved 2026-05-06 (modulo the actual reliability proof at next 06:00 UTC scheduled fire)
- **Symptom:** S5 deployed `pipelineiq-functions-dev` with cron `0 0 6 * * *`. Inspection of `velora_oms` 5 days later showed only one Function-produced day (day 22, `created_at = 2026-05-01 11:19:57`); subsequent scheduled fires (May 2 06:00, May 3 06:00, ..., May 6 06:00) all silent. App Insights had **zero telemetry of any kind** for 10 days — no requests, no traces, no exceptions. Function App reported `state: Running`, function `enabled: true`, schedule unchanged. `az monitor activity-log` only showed manual management operations. Manual invoke via `admin/functions/generator` worked instantly (HTTP 202, day 23/24 landed in DB) — proving the function code itself was fine.
- **Root cause:** **Linux Consumption (Y1) + non-HTTP triggers + scale-to-zero is a documented unreliable path.** When the Function App has no HTTP traffic, the platform scales the host instance to zero. On Linux Consumption specifically, the ScaleController is supposed to wake the host for timer triggers but frequently misses them. Windows Consumption handles this reliably; Linux does not. The 2026-05-01 11:19 fire was actually a `runOnStartup`-style invocation triggered by the deploy itself, not the cron schedule (11:19 UTC ≠ 06:00 UTC).
- **Fix:** Migrated Function App from Y1 (Linux Consumption) → FC1 (Flex Consumption). DECISIONS #50. `core/functions/main.tf` swapped `azurerm_linux_function_app` for `azurerm_function_app_flex_consumption`, plan SKU Y1 → FC1, added private blob container `app-package-pipelineiq-functions-dev` for the Flex deployment package. Required `terraform apply -replace=module.functions.azurerm_service_plan.this` (Azure rejects in-place SKU change `Dynamic → FlexConsumption`). After destroy/recreate, MSI principal_id changed (`ad0af497-...` → `ccdac37d-...`), needed `DROP USER` on velora_oms then `scripts/grant_function_msi_sql.py` to re-grant. Redeployed via existing `scripts/deploy_function.sh` (171s remote build).
- **Prevention / first-check:** **For any Linux Function App with non-HTTP triggers (timer, queue, blob, event grid), use Flex Consumption (FC1), not Linux Consumption (Y1).** Pricing is effectively the same under the 100K GB-s/month free grant. If you find yourself diagnosing "timer didn't fire" on Linux Consumption, don't bother debugging the cron expression — it's almost certainly the platform scale-to-zero issue. Telemetry signals: zero rows in App Insights `requests`/`traces` for the period, but `admin/host/status` returns `state: Running`. Combined with `az functionapp function show` confirming `isDisabled: false` and the schedule field correct, you've ruled out config; it's the plan tier.
- **References:** DECISIONS #50, DECISIONS #51, PROGRESS S6 (2026-05-06), `PipelineIQ-IaC/core/functions/{main.tf,outputs.tf}` commit `fe45547` (local-only pending GitHub auth fix).

### #16 — 2026-05-01 — Cluster-policy autotermination invalid for job clusters
- **Phase / Session:** Phase 2 kickoff, Session 5
- **Category:** infra
- **Severity:** minor
- **Effort:** ~5 min
- **Status:** resolved 2026-05-01
- **Symptom:** First Bronze smoke-test submit failed with
  `InvalidParameterValue: Automated clusters do not support autotermination`.
- **Root cause:** The cluster policy in `core/databricks_uc/main.tf` enforces
  `autotermination_minutes = 30` (correct for interactive / all-purpose
  clusters that humans leave running). Job clusters self-terminate when the
  job ends, so they reject any autotermination setting — and the policy was
  forcing one onto them via `policy_id`.
- **Fix:** in `scripts/run_bronze_smoke.py`, build the `compute.ClusterSpec`
  without `policy_id` — just `num_workers=1` + `Standard_DS3_v2` +
  `data_security_mode=SINGLE_USER`. The policy still applies to interactive
  clusters created via the Databricks UI, which is its intended scope.
- **Prevention / first-check:** Cluster policies have two scopes —
  interactive (all-purpose) and jobs. `autotermination_minutes` is interactive-
  only. When ADF (Tier 6) lands, decide whether ADF triggers job clusters
  (cheap, no policy needed) or interactive cluster reuse (binds to policy +
  needs the policy to allow auto-stop). Document in DECISIONS at that point.
- **References:** DECISIONS #48, `scripts/run_bronze_smoke.py`.

### #15 — 2026-05-01 — `partitionBy()` rejects Column expressions, requires names
- **Phase / Session:** Phase 2 kickoff, Session 5
- **Category:** code
- **Severity:** minor
- **Effort:** ~3 min
- **Status:** resolved 2026-05-01
- **Symptom:** Bronze notebook job failed with PySpark `NOT_ITERABLE: Column
  is not iterable`, pointing at the `.write.partitionBy(...)` call. Error
  message gave no hint about the actual rule violation.
- **Root cause:** `df.write.partitionBy(F.col("_ingestion_timestamp")
  .cast("date").alias("_ingestion_date"))` — passed a `Column` expression
  to `partitionBy`. The DataFrameWriter's `partitionBy` only accepts string
  column names, not expressions. Spark internally tries to iterate the value
  as if it were already a name, hits the `NOT_ITERABLE` path.
- **Fix:** derive the partition column as a separate `withColumn` step before
  the write, then `partitionBy("_ingestion_date")` by name.
  ```python
  df = df.withColumn("_ingestion_date", F.to_date(F.col("_ingestion_timestamp")))
  df.write.partitionBy("_ingestion_date").saveAsTable(...)
  ```
- **Prevention / first-check:** When PySpark throws `NOT_ITERABLE: Column is
  not iterable`, the first check is **whether you passed a Column object to
  a method that expects a string column name**. Common offenders: `partitionBy`,
  `bucketBy`, `pivot`. Method signatures don't error at parse time because
  `*cols: str | Column` is permissive in pyspark stubs but stricter at runtime.
- **References:** `notebooks/bronze/ingest_to_bronze.py`.

### #14 — 2026-05-01 — UC metastore creation hit "1 per region per account" limit
- **Phase / Session:** Phase 0 finish, Session 5
- **Category:** infra
- **Severity:** major
- **Effort:** ~15 min (5 min apply error → 10 min Terraform refactor)
- **Status:** resolved 2026-05-01
- **Symptom:** First `terraform apply` of `core/databricks_uc/` partially
  succeeded (access connector + role assignment + cluster policy + secret
  scope + SQL warehouse all applied), then failed on
  `databricks_metastore.this`:
  `cannot create metastore: This account with id 95652d59-... has reached
  the limit for metastores in region centralindia.`
- **Root cause:** Databricks enforces **1 metastore per Azure region per
  account**. When `mohan.gowda` was promoted to Account Admin and first
  signed into `https://accounts.azuredatabricks.net`, Databricks silently
  auto-created `metastore_azure_centralindia` (a system-owned metastore)
  for the tenant. Workspace was already auto-assigned to it
  (`metastore_assignment_status: AUTO_ASSIGNMENT_ENABLED`). Terraform's
  attempt to create a second one was rejected by the platform.
- **Fix:** swap `databricks_metastore "this"` from a `resource` to a `data`
  source referencing the existing metastore_id. Drop
  `databricks_metastore_assignment` (workspace already auto-assigned).
  Pass `metastore_id` literal from `clients/velora/main.tf`. Catalogs +
  external locations + storage credential land normally under the existing
  metastore — they're catalog/location-scoped, not metastore-scoped.
  This collapsed the planned Stage 1 / Stage 2 split (DECISIONS #45) into
  a single clean apply (DECISIONS #46). Stage 1 / 2 split obsolete.
- **Prevention / first-check:** When provisioning a Databricks workspace
  in a new region for an existing tenant, **always check for an existing
  system metastore first** before writing Terraform to create one:
  ```bash
  curl -s -X GET "https://accounts.azuredatabricks.net/api/2.0/accounts/{ACCOUNT_ID}/metastores" \
    -H "Authorization: Bearer $(az account get-access-token --resource 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d --query accessToken -o tsv)"
  ```
  If a metastore exists for the region, adopt via data source. If not,
  create normally. This applies to any future region (e.g. Sail expanding
  into eastus2 for a second tenant) — `eastus2` and `uksouth` already had
  system metastores in this account from prior signed-in sessions.
- **References:** DECISIONS #46, supersedes-rationale-of #45,
  `core/databricks_uc/main.tf`.

### #13 — 2026-04-22 — Stores missing from `seed_to_db` — silent orphan data
- **Phase / Session:** Phase 1 verification, Session 4
- **Category:** data
- **Severity:** major
- **Effort:** ~20 min (caught in post-task data-quality sweep)
- **Status:** resolved 2026-04-22
- **Symptom:** `velora_pim.stores` was empty after a "successful" catalogue
  seed. 770 STORE-channel orders and 1.32M inventory snapshot rows held
  `store_id` values with no matching row. No FK constraint caught it —
  Azure SQL accepted the rows silently.
- **Root cause:** `catalogue.py::seed_to_db` bulk-inserted categories,
  products, pricing, sales_reps, territory_assignments — but **skipped**
  stores. `_build_stores()` built the DataFrame, `build_catalogue()`
  returned it, but the write-to-DB step never referenced it. SCHEMA.md
  also omitted the stores spec (fixed in 2026-04-22 SCHEMA change #2),
  so no upstream spec forced the author to notice.
- **Fix:** added `_bulk_insert(cursor, "velora_pim.stores", [...],
  catalogue["stores"])` between `product_pricing` and `sales_reps`.
  Backfilled 45 rows in-session. Referential integrity now clean.
- **Prevention / first-check:** run the 5-orphan-join verification sweep
  (orders→customers, lines→orders, lines→products, status_log→orders,
  inventory→products, **orders→stores**) at the end of every seed — row
  counts alone don't catch silent orphans.
- **References:** DECISIONS #44, SCHEMA change log #2.

### #12 — 2026-04-22 — Day 21 `schema_drift` failure injection was a no-op at Azure SQL
- **Phase / Session:** Phase 1, Session 4, task 1
- **Category:** data (architectural)
- **Severity:** major
- **Effort:** ~30 min to diagnose, triggered a phase-boundary decision
- **Status:** resolved architecturally 2026-04-22 (deferred to Phase 3)
- **Symptom:** `python generator/main.py --date 2026-01-21 --failure
  schema_drift` ran to success. Day 21 committed as clean data.
- **Root cause:** `failure_injector.py::inject_schema_drift` adds a
  `promo_code` column to the orders DataFrame, but `orders.py` uses an
  explicit column list in its INSERT — pandas silently dropped the
  extra column before the TDS batch ever left the laptop. Deeper cause:
  the injector was designed for **Parquet** (schema-on-read) sinks —
  its docstring spells this out. Relational sources don't spontaneously
  grow columns in real life; those anomalies appear during extraction.
- **Fix:** no generator-layer fix. Failure injection relocated to the
  landing / ADF layer per DECISIONS #43 — the generator stays a faithful
  source-system simulator. All 6 failure classes re-implemented in Phase 3
  against Parquet files in `landing/{entity}/date=...`.
- **Prevention / first-check:** when a "failure" run produces zero
  observable bad data, check whether the mutation is compatible with
  the sink's schema enforcement **before** assuming the injector worked.
- **References:** DECISIONS #43, PROGRESS Session 4 Broke #5.

### #11 — 2026-04-22 — `scripts/backfill_inventory.py` v1 was 15× too slow
- **Phase / Session:** Phase 1 recovery tooling, Session 4
- **Category:** perf
- **Severity:** minor
- **Effort:** ~25 min (killed v1 at 41K rows, refactored)
- **Status:** resolved 2026-04-22
- **Symptom:** Backfill throughput 76 rows/sec (≈46 min per day). Killed
  at 41K / 189K rows.
- **Root cause:** Opened a fresh pyodbc connection + AAD auth round-trip
  per 250-row chunk. TCP + auth overhead drowned the actual inserts.
- **Fix:** Reuse one connection across all chunks; only reconnect on a
  transient `OperationalError`. Throughput jumped to 222 rows/sec
  (14 min/day) with no loss of resilience — autocommit-per-chunk and
  retry-with-reconnect still intact.
- **Prevention / first-check:** for any chunked writer against Azure
  SQL over VPN, keep the connection long-lived and only reconnect on
  failure. The laptop→centralindia RTT (~200 ms) amplifies any
  per-chunk overhead.
- **References:** PROGRESS Session 4 Broke #4; `scripts/backfill_inventory.py`.

### #10 — 2026-04-22 — Inventory snapshot single-transaction TCP reset
- **Phase / Session:** Phase 1, Session 4, task 1
- **Category:** network
- **Severity:** blocker (partial seed, recoverable)
- **Effort:** ~90 min including tool build
- **Status:** resolved 2026-04-22
- **Symptom:** Day 17 and day 21 inventory writes died mid-transaction
  with `TCP Provider: Error 0x274C (WSAETIMEDOUT)` / `0x20 (WSAENETRESET)`
  after ≈3 min of the 6-min single-transaction 189 K-row write. Azure
  auto-rolled back (row count = 0 for those dates); main batches
  (customers/orders/status) had already committed cleanly.
- **Root cause:** VPN + residential ISP + Azure SQL serverless + single
  6-min transaction = too many independent failure surfaces for one
  uninterrupted connection. Any of them blinking kills the whole write.
- **Fix:** new `scripts/backfill_inventory.py` with per-chunk autocommit
  (250-row chunks, one `DELETE + INSERT` per chunk per `snapshot_date`)
  and retry-with-reconnect (5 attempts, exponential backoff 2/4/8/16/32 s).
  A failure now loses at most the in-flight chunk and retries automatically.
- **Prevention / first-check:** never wrap an entire 189 K-row inventory
  write in a single transaction over a high-latency / VPN link.
  Autocommit-per-chunk is the default going forward. This lesson should
  carry into the Bronze notebook's write strategy (append mode, small
  commits) once Phase 2 lands.
- **References:** DECISIONS #42 (related — timeout context); PROGRESS Session 4
  Broke #3; `scripts/backfill_inventory.py`.

### #9 — 2026-04-22 — Generator RNG seed constant across dates → PK collision on day 2+
- **Phase / Session:** Phase 1, Session 4, task 1
- **Category:** code
- **Severity:** blocker (first multi-day seed attempt)
- **Effort:** ~40 min diagnose + fix + re-verify
- **Status:** resolved 2026-04-22
- **Symptom:** Day 16 (second-ever seed date) crashed with
  `pyodbc.IntegrityError 23000: Violation of PRIMARY KEY constraint
  PK_customers. Duplicate key value is (00000000-0000-0000-53c8-ffe06310fde6)`.
  Tell-tale all-zero upper bytes from the UUID construction. 15-for-15
  collision between days 15 and 16.
- **Root cause:** `main.py::run` used a constant `seed=42` regardless
  of `--date`. Every day's RNG stream consumed identical numbers in
  identical order → identical customer/address UUIDs. Session 3
  verification was single-date and never tripped this.
- **Fix:** `effective_seed = seed + run_date.toordinal()` — date-derived
  seed for both `np.random.default_rng(...)` and `Faker.seed(...)`.
  Same date → same output (reproducibility preserved). Different dates
  → non-overlapping UUID space. CLI `--seed` still honoured as the base.
  Catalogue is unaffected because catalogue IDs are UUID5 (DECISIONS #19).
- **Prevention / first-check:** any generator with PRNG-derived PKs
  must seed per-date or per-run. Default-constant seeds are fine for a
  single-day demo, fatal for multi-day seeds.
- **References:** DECISIONS #41; PROGRESS Session 4 Broke #2.

### #8 — 2026-04-22 — Azure SQL serverless cold start exceeds pyodbc 30 s timeout
- **Phase / Session:** Phase 1, Session 4, task 1
- **Category:** infra
- **Severity:** blocker (first-connect failure)
- **Effort:** ~15 min (including Azure CLI diagnosis)
- **Status:** resolved 2026-04-22
- **Symptom:** First run of the day returned `[HYT00] Login timeout
  expired (0)` — misleadingly *not* a `Login failed` error. pyodbc's
  30-second default tripped before the DB had finished resuming.
- **Root cause:** `velora_oms` is on the serverless tier with a 60-minute
  auto-pause. Wake-up took 54 s — confirmed via `az sql db show
  --query resumedDate`.
- **Fix:** `Connection Timeout=90` in the pyodbc connection string
  (`generator/config.py::get_connection_string`). 90 s is the documented
  upper bound for serverless cold start.
- **Prevention / first-check:** on any "timeout expired" error against
  Azure SQL serverless, run `az sql db show --query status` **first** —
  if `Paused`, the DB is resuming, not broken. Any new pyodbc connection
  string against this server must carry the 90 s timeout.
- **References:** DECISIONS #42; PROGRESS Session 4 Broke #1.

### #7 — 2026-04-21 — Generator cursor-vs-conn + missing `fast_executemany`
- **Phase / Session:** Phase 1 verification, Session 3
- **Category:** code
- **Severity:** blocker (Phase 1 end-to-end verification)
- **Effort:** ~45 min across two attempts
- **Status:** resolved 2026-04-21 (commit `7149cb4`)
- **Symptom:** First attempt at live Azure SQL seed — `main.py::run`
  called `fast_executemany` on a connection object instead of a cursor,
  raising `AttributeError`. Second attempt past that bug — the 4,200
  product inserts in `catalogue.py::_bulk_insert` hit `[08S01] TCP
  Provider: Error 0x274C` (communication link failure) before the batch
  completed.
- **Root cause:** `fast_executemany` is a **cursor-level** attribute,
  not a connection-level one. Without it, pyodbc sends one TDS packet
  per row; over a laptop → centralindia link (~200 ms RTT), 4,200 inserts
  exceed the connection's network timeout.
- **Fix:** set `cursor.fast_executemany = True` at each of the three
  cursor-creation sites: `catalogue.py::_bulk_insert`, `main.py::run`
  (main transaction), `main.py::_write_inventory` (inventory snapshot).
  189 K inventory rows went from unrecoverable to ~5 min.
- **Prevention / first-check:** any new pyodbc cursor anywhere in this
  codebase must set `fast_executemany = True` before any `executemany`.
  Documented in DECISIONS #40 as a load-bearing convention.
- **References:** DECISIONS #40; commit `7149cb4`; PROGRESS Session 3.

### #6 — 2026-04-21 — Portal AI returning HTTP 401 — env-var key didn't match any live key
- **Phase / Session:** Portal demo restoration, Session 2
- **Category:** auth
- **Severity:** blocker (live demo broken)
- **Effort:** ~3 hr — the worst time-to-diagnose of the project so far
- **Status:** resolved 2026-04-21 (all speculative commits reverted)
- **Symptom:** `/api/generate-incident` returned HTTP 500 wrapping an
  Azure 401. Five speculative fixes attempted — switching `fetch` ↔ SDK,
  `api-key` header ↔ `Authorization: Bearer`, `chat/completions` ↔
  `/responses`, `openai.azure.com` ↔ `services.ai.azure.com`. All failed
  identically.
- **Root cause:** the `AZURE_OPENAI_API_KEY` value stored in Vercel
  Production did not match either Key 1 or Key 2 on the actual
  `pipeline-iq-resource` — likely a stale snapshot from pre-rotation.
  The code was correct from the start. 5 commits were pure noise.
- **Fix:** retrieved live Key 1 via `az cognitiveservices account keys
  list ...`, curl-tested it against the existing endpoint (HTTP 200
  immediately), then rotated the Vercel env var via `vercel env rm` +
  `vercel env add --sensitive`. Reverted all 5 speculative commits.
- **Prevention / first-check:** **on a first-time 401, run a raw `curl`
  against the endpoint with a freshly-retrieved key before touching any
  code.** If curl succeeds, the problem is the stored credential, not
  the code. This single discipline would have saved ~2.5 hr.
- **References:** DECISIONS #34; PROGRESS Session 2 Broke.

### #5 — 2026-04-21 — Bootstrap SQL blocked — laptop IP not in either server's firewall
- **Phase / Session:** Phase 0, Session 3 (session-crossing into 3 proper)
- **Category:** network
- **Severity:** blocker (bootstrap SQL couldn't run)
- **Effort:** ~45 min, spanned sessions
- **Status:** resolved 2026-04-21 (same session, after hook guidance)
- **Symptom:** `psql` + `sqlcmd` from laptop both failed — Postgres
  connection silently timed out, Azure SQL returned ambiguous "Login
  failed". `curl ifconfig.me` to get the IP was blocked by a hook
  (IP-exfil concern). `az ... firewall-rule create` with `0.0.0.0/0`
  was also correctly blocked by a hook (security weakening).
- **Root cause:** only `allow-azure-services` (0.0.0.0–0.0.0.0) was in
  the firewall rule set. Laptop public IP had never been added.
- **Fix:** user pasted the IP (`69.5.168.130` — stable VPN dedicated IP,
  see `memory/project_vpn_dedicated_ip.md`). Added to both PG and SQL
  firewall rules via Terraform apply (`current_ip` variable on both
  modules). Bootstrap SQL ran cleanly after.
- **Prevention / first-check:** add the VPN IP to `terraform.tfvars`
  at the start of any new environment; don't discover it by hitting
  a timeout mid-flow. Since the IP is a dedicated VPN IP and stable,
  this is a one-time setup per tfvars file.
- **References:** DECISIONS #35 (related — Owner elevation same session);
  `memory/project_vpn_dedicated_ip.md`; PROGRESS Session 3 Broke.

### #4 — 2026-04-21 — `azure.extensions` Postgres server parameter is case-sensitive
- **Phase / Session:** Phase 0 Tier 4, Session 3
- **Category:** infra
- **Severity:** major (17 of 18 resources landed; one config failed)
- **Effort:** ~10 min
- **Status:** resolved 2026-04-21
- **Symptom:** Tier 3/4 apply error —
  `ServerParameterToCMSUnAllowedParameterValue` on
  `azure.extensions = "VECTOR,PG_TRGM,UUID_OSSP"`. The error helpfully
  listed every valid lowercase value.
- **Root cause:** Azure's docs and most examples show uppercase, but
  the allowlist enum is defined lowercase internally. Case-sensitive
  match.
- **Fix:** changed `core/postgres/variables.tf` default to lowercase
  `vector,pg_trgm,uuid-ossp` (note the hyphen in `uuid-ossp`, not
  underscore). Re-plan → 1 resource diff. Re-apply clean.
- **Prevention / first-check:** allowlisting ≠ creating. After apply,
  `bootstrap_postgres.sql` still runs `CREATE EXTENSION IF NOT EXISTS
  vector`. If an extension fails to create, check both the server
  parameter (lowercase) and the `CREATE EXTENSION` call.
- **References:** DECISIONS #37; PROGRESS Session 3.

### #3 — 2026-04-21 — `grant_current_user_*` RBAC failed — Contributor can't write role assignments
- **Phase / Session:** Phase 0 Tier 2, Session 3 (first apply of the session)
- **Category:** auth
- **Severity:** blocker (apply stalled with partial state)
- **Effort:** ~15 min + propagation
- **Status:** resolved 2026-04-21
- **Symptom:** First apply attempt — Key Vault + Log Analytics + ADLS
  created, but the two role assignments (KV Secrets Officer, Storage
  Blob Data Owner) failed with HTTP 403. The 5 filesystem creations
  that depended on those grants also failed.
- **Root cause:** the running principal had **Contributor** only.
  `Microsoft.Authorization/roleAssignments/write` requires **Owner**
  (or a custom role). Sole existing Owner was the client admin account.
- **Fix:** user Portal-elevated `mohan.gowda@SailAnalyticsAP.onmicrosoft.com`
  from Contributor → Owner at subscription scope. ~2 min including
  propagation. Re-plan + re-apply clean.
- **Prevention / first-check:** before the first apply of any new tier
  that uses `grant_current_user_*`, run `az role assignment list
  --assignee <principal> --query "[].roleDefinitionName"` and confirm
  `Owner` is present. Least-privilege alternative documented in
  DECISIONS #35 tail (RG-scoped User Access Administrator).
- **References:** DECISIONS #35; PROGRESS Session 3 Broke #1.

### #2 — 2026-04-21 — Terraform no longer in Homebrew core
- **Phase / Session:** Phase 0, Session 1
- **Category:** tooling
- **Severity:** minor
- **Effort:** ~5 min
- **Status:** resolved 2026-04-21
- **Symptom:** `brew install terraform` exited 0 but then `terraform
  --version` → `command not found`.
- **Root cause:** HashiCorp moved Terraform out of Homebrew core after
  the BSL license change. The core formula is now an empty shim.
- **Fix:** `brew install hashicorp/tap/terraform`.
- **Prevention / first-check:** document the tap install in any new
  environment setup doc. Low recurrence risk per developer.
- **References:** PROGRESS Session 1 Broke.

### #1 — 2026-04-21 — `msodbcsql18` brew install deadlocked on EULA
- **Phase / Session:** Phase 0, Session 1 → Session 3
- **Category:** tooling
- **Severity:** blocker (spanned sessions)
- **Effort:** 55 min lost in Session 1; ~5 min to fix in Session 3
- **Status:** resolved 2026-04-21 (Session 3)
- **Symptom:** `ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18`
  hung interactively for 55 minutes. Had to be killed. pyodbc + sqlcmd
  unusable until resolved.
- **Root cause:** wrong env var. `ACCEPT_EULA=Y` is Microsoft's
  installer-script variable. Homebrew needs `HOMEBREW_ACCEPT_EULA=Y`
  to skip the interactive prompt during formula install.
- **Fix:** `HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18`
  completed in seconds. Installs landed at `/opt/homebrew/bin/{sqlcmd,bcp}`.
- **Prevention / first-check:** add `HOMEBREW_ACCEPT_EULA=Y` to any
  repo onboarding script that installs Microsoft-licensed brew formulae.
- **References:** PROGRESS Session 1 Broke, Session 3 Built.

---

*Maintained at the end of every session per CLAUDE.md's end-of-session
checklist. Add an entry immediately after the blocker clears — deferring
loses the diagnosis detail.*
