# Runbook — Databricks Account Admin Bootstrap

**One-time procedure per Azure tenant.** Required before Terraform can
create a Unity Catalog metastore. Blocker for Tier 5.2–5.4 in
`docs/build_order.md`. See DECISIONS #45 for architectural context.

**Who runs this:** A Sail AAD Global Administrator (currently
`admin@SailAnalyticsAP.onmicrosoft.com`). Workspace admins cannot run
this — account-admin privileges are a separate authority tier from
workspace-admin, and only a tenant Global Admin can bootstrap the
Databricks account on first use.

**Time:** ~5 minutes end-to-end.

**Side effect:** First run also activates the Databricks account for
the entire Sail tenant (not just this workspace). This is a one-time,
tenant-level activation — safe and expected.

---

## Steps

### 1. Open the Databricks account console in a private window

```
https://accounts.azuredatabricks.net
```

Use a **private/incognito** browser window. This avoids profile
contamination if the admin account and your regular account are both
logged into Microsoft services on the same browser profile.

### 2. Sign in as the Global Admin

User: `admin@SailAnalyticsAP.onmicrosoft.com`
MFA prompt expected.

### 3. First-time activation (skip if already activated)

If this is the tenant's first login to `accounts.azuredatabricks.net`,
you'll see a **Set up your Databricks account** flow. Fill in:

| Field | Value |
|---|---|
| Country/region | India |
| Account name | Sail Analytics *(cosmetic — change later if desired)* |
| Terms | Accept |

Click **Continue**. The account is now activated for the Sail tenant.

If instead you land directly on a workspace list, the account was
already activated — skip to step 4.

### 4. Promote the target user to Account Admin

In the left sidebar:

1. Click **User management** (or **Users** depending on UI version)
2. Click **Add user** (top-right)
3. Email: `mohan.gowda@SailAnalyticsAP.onmicrosoft.com`
   *(exact AAD UPN, lowercase)*
4. Click **Send invite** / **Add**
5. Once the user appears in the list, click the user row
6. Go to the **Roles** tab
7. Toggle **Account admin** → on
8. Click **Save**

### 5. Verify the workspace is attached

Still in the account console:

1. Click **Workspaces** in the left sidebar
2. Confirm `pipelineiq-dbx-dev` is in the list
3. Click it → note the workspace URL (should be
   `adb-7405617631498102.2.azuredatabricks.net`)

If the workspace doesn't appear, the Azure-side provisioning isn't
linked to the Databricks account — escalate before proceeding (this
should never happen for a workspace created via the Azure portal or
Terraform with a valid subscription).

### 6. Sign out

Sign out of `admin@...` from the account console and close the private
window. **Nothing further is required with this admin identity** — all
subsequent Databricks work happens as `mohan.gowda` via AAD auth from
Terraform.

---

## Sanity check from the target user's side

After the admin completes step 4, the newly-promoted user should:

```bash
# open the workspace in a normal browser
open https://adb-7405617631498102.2.azuredatabricks.net
```

Sign in as `mohan.gowda@SailAnalyticsAP.onmicrosoft.com`. Click the
email avatar in the top-right corner. **"Manage Account"** should now
be visible as a menu item. Clicking it should take you into the
account console with full admin rights.

If "Manage Account" does not appear:

- Check that the invite was accepted — the user may need to accept an
  email invitation before the role takes effect
- Confirm the role toggle is actually on in the account console
- Wait 1–2 minutes for role propagation, refresh the workspace page

---

## What this unblocks

Once complete, the following become possible from Terraform with AAD
auth (no PAT, no password):

- `databricks_metastore` — create the UC metastore (region-scoped, one
  per region)
- `databricks_metastore_assignment` — attach metastore to workspace
- `databricks_external_location` — 5 locations for landing, bronze,
  silver, gold, quarantine
- `databricks_catalog` — 3 catalogs (`bronze`, `silver`, `gold`)
- `databricks_schema`, `databricks_grant`, `databricks_external_metadata`

Corresponds to `core/databricks_uc/` **Stage 2** per DECISIONS #45.
Run `terraform plan` + `apply` from `PipelineIQ-IaC/clients/velora/`
after adding the Stage 2 resources to the module.

## Migration from `hive_metastore` to UC catalogs

Bronze/Silver/Gold tables that shipped in Stage 1 on the default
`hive_metastore` catalog migrate to UC with one DDL per table:

```sql
-- In a Databricks notebook or SQL Warehouse
CREATE TABLE bronze.orders
USING DELTA
LOCATION 'abfss://bronze@pipelineiqadlsdev.dfs.core.windows.net/orders/';
```

The underlying Delta folder stays put; the table registration moves
from `hive_metastore.bronze.orders` to `bronze.orders` (UC catalog).
After verification, drop the hive_metastore copy:

```sql
DROP TABLE hive_metastore.bronze.orders;
```

This does **not** delete the data — Delta at `abfss://...` is untouched.
Only the metastore entry goes.

---

## Related

- **DECISIONS #45** — the two-stage UC rollout strategy
- **`docs/build_order.md`** Tier 5.2–5.8 — per-item status tracking
- **`docs/incident_log.md`** — add an entry here only if the bootstrap
  fails in a way that took >15 min to diagnose
