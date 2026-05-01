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
