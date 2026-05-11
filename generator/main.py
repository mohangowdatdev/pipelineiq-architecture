"""
PipelineIQ Velora Data Generator — entry point.

Runs as an Azure Function (timer trigger, 6am daily) or as a CLI for
testing and historical backfill.

Execution order:
  1. Load config and initialise random state
  2. Connect to Azure SQL
  3. Seed catalogue if first run (products, stores, reps)
  4. Load catalogue and existing customer pool into memory
  5. Generate customers (new registrations + SCD updates)
  6. Generate orders and order_lines
  7. Generate status updates for existing orders
  8. Generate dimension changes (prices, launches, rep reassignments)
  9. Apply failure injection if requested
 10. Write all generated data to Azure SQL in a single transaction
 11. Write inventory snapshot (full refresh, 189k rows — separate transaction)
 12. Log run summary and exit

CLI usage:
  python main.py --date 2025-01-15 --dry-run
  python main.py --date 2025-01-15 --failure schema_drift
  python main.py --seed 999

Azure Function: Azure Functions runtime calls main(mytimer) automatically.
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from typing import Dict, Optional

# Ensure sibling modules (config, catalogue, customers, …) are importable
# whether this file is run as a CLI script (`python generator/main.py ...`)
# or imported by Azure Functions as `generator.main` (where the package dir
# isn't on sys.path by default).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import numpy as np
import pyodbc
from dotenv import load_dotenv
from faker import Faker

import config
import catalogue as cat_module
import customers as cust_module
import orders as orders_module
import status_updates as status_module
import dimension_changes as dim_module
import failure_injector as fi

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("generator.main")


def _connect_with_resume_retry(
    conn_str: str,
    max_attempts: int = 12,
    initial_backoff_s: float = 10.0,
    max_backoff_s: float = 30.0,
) -> "pyodbc.Connection":
    """
    pyodbc.connect with retry on Azure SQL serverless wake-up errors.

    Azure SQL serverless auto-pauses when idle. On the first connect after
    pause, two distinct failure modes can fire before the DB is ready:

      1. HYT00 "Login timeout expired" — connect itself times out before
         the server even responds. Mitigated by `timeout=90` kwarg
         (DECISIONS #42 / #50 addendum).
      2. 40613 "Database '<x>' on server '<y>' is not currently available"
         — TCP+TLS+auth handshake succeeds within ~0.1s, then the server
         immediately returns this error because it's still warming up.
         `Connection Timeout` cannot help: the server has already replied.
         Only application-level retry works. Caused S10 5/10 + 5/11
         autonomous fires to fail at ~15s after Logic-App-driven invoke.

    Retries up to `max_attempts` times with exponential backoff capped at
    `max_backoff_s`. Total wall-time budget ≈ 5 min — well inside the
    30-min function timeout.
    """
    import pyodbc as _pyodbc
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _pyodbc.connect(conn_str, timeout=90)
        except _pyodbc.Error as e:
            msg = str(e)
            transient = (
                "40613" in msg
                or "is not currently available" in msg
                or "HYT00" in msg
                or "Login timeout expired" in msg
            )
            if not transient:
                raise
            last_err = e
            if attempt == max_attempts:
                break
            backoff = min(initial_backoff_s * (1.5 ** (attempt - 1)), max_backoff_s)
            logger.warning(
                "Connect attempt %d/%d hit transient SQL serverless wake-up error "
                "(%s) — sleeping %.0fs before retry",
                attempt, max_attempts, msg.split("\n")[0][:160], backoff,
            )
            time.sleep(backoff)
    assert last_err is not None
    raise last_err


def run(
    run_date: date,
    dry_run: bool = False,
    failure_type: Optional[str] = None,
    seed: int = config.RANDOM_SEED,
) -> Dict:
    """
    Main orchestration function.

    Returns a dict of record counts per table for logging and test assertions.
    """
    effective_seed = seed + run_date.toordinal()
    logger.info(
        "Generator run: date=%s dry_run=%s failure=%s seed=%d effective_seed=%d",
        run_date, dry_run, failure_type, seed, effective_seed,
    )

    rng = np.random.default_rng(effective_seed)
    Faker.seed(effective_seed)
    fake = Faker(["en_IN"])

    # Connect with retry on Azure SQL serverless wake-up errors (40613,
    # HYT00). See `_connect_with_resume_retry` docstring for the failure
    # modes — connect-side timeout (HYT00) and post-connect 40613 are
    # both possible and need distinct handling. Together they covered
    # every cold-start failure observed in S5/S6/S10 scheduled fires.
    conn = _connect_with_resume_retry(config.get_connection_string())
    conn.autocommit = False

    try:
        # ── 0. Idempotency guard ─────────────────────────────────────────────
        # Skip if this run_date already has data in the source. Prevents PK
        # collisions on re-fires (manual + Function on same date) and makes
        # the daily timer naturally idempotent. To genuinely re-seed a date,
        # wipe its rows from velora_oms first.
        guard_cur = conn.cursor()
        guard_cur.execute(
            "SELECT COUNT(*) FROM velora_oms.orders WHERE order_date = ?",
            run_date,
        )
        existing = guard_cur.fetchone()[0]
        if existing > 0:
            logger.warning(
                "Date %s already has %d orders in velora_oms — skipping (no-op). "
                "To re-seed, wipe this date from velora_oms first.",
                run_date, existing,
            )
            return {"_skipped": True, "existing_orders": existing}

        # ── 1. Seed catalogue on first run ────────────────────────────────────
        if not cat_module.is_catalogue_seeded(conn):
            logger.info("Catalogue not seeded — generating and seeding now")
            catalogue = cat_module.build_catalogue(seed=seed)
            if not dry_run:
                cat_module.seed_to_db(catalogue, conn)
                conn.commit()
                logger.info("Catalogue seeded successfully")
        else:
            catalogue = None  # loaded from DB below

        # ── 2. Load catalogue from DB ─────────────────────────────────────────
        live_catalogue = cat_module.load_from_db(conn)
        products_df = live_catalogue["products"]
        pricing_df  = live_catalogue["pricing"]
        stores_list = config.STORES
        reps_df     = live_catalogue["reps"]

        # ── 3. Load existing customers ────────────────────────────────────────
        existing_customers_df = cust_module.load_existing_customers(conn)
        all_cust_ids   = existing_customers_df["customer_id"].tolist()
        d2c_cust_ids   = existing_customers_df[
            existing_customers_df["account_type"] == "D2C"
        ]["customer_id"].tolist()
        b2b_cust_ids   = existing_customers_df[
            existing_customers_df["account_type"] == "B2B"
        ]["customer_id"].tolist()

        # ── 4. Generate all data ──────────────────────────────────────────────
        new_cust_count = config.get_adjusted_volume(
            config.VOLUME_NEW_CUSTOMERS_MIN,
            config.VOLUME_NEW_CUSTOMERS_MAX,
            run_date, rng,
        )
        new_customers_df, new_addresses_df = cust_module.generate_new_customers(
            count=new_cust_count,
            cities=config.CITIES,
            rng=rng,
            fake=fake,
            run_date=run_date,
        )

        upd_count = config.get_adjusted_volume(
            config.VOLUME_CUSTOMER_UPDATES_MIN,
            config.VOLUME_CUSTOMER_UPDATES_MAX,
            run_date, rng,
        )
        customer_updates_df = cust_module.generate_customer_updates(
            existing_df=existing_customers_df,
            count=upd_count,
            rng=rng,
            cities=config.CITIES,
            run_date=run_date,
        )

        # Combine existing + new customer IDs so new-customer orders are possible
        new_cust_ids = new_customers_df["customer_id"].tolist() if not new_customers_df.empty else []
        all_cust_ids_combined   = all_cust_ids + new_cust_ids
        d2c_new = new_customers_df[new_customers_df["account_type"] == "D2C"]["customer_id"].tolist() if not new_customers_df.empty else []
        b2b_new = new_customers_df[new_customers_df["account_type"] == "B2B"]["customer_id"].tolist() if not new_customers_df.empty else []
        d2c_cust_ids_combined = d2c_cust_ids + d2c_new or all_cust_ids_combined
        b2b_cust_ids_combined = b2b_cust_ids + b2b_new or all_cust_ids_combined

        orders_df, order_lines_df = orders_module.generate_orders(
            run_date=run_date,
            all_customer_ids=all_cust_ids_combined or ["PLACEHOLDER"],
            d2c_customer_ids=d2c_cust_ids_combined,
            b2b_customer_ids=b2b_cust_ids_combined,
            products_df=products_df,
            pricing_df=pricing_df,
            stores=stores_list,
            reps_df=reps_df,
            rng=rng,
        )

        open_orders_df = status_module.load_open_orders(conn, run_date)
        delivered_df   = status_module.load_recently_delivered_orders(conn, run_date)
        status_log_df, order_updates_df = status_module.generate_status_updates(
            open_orders_df=open_orders_df,
            delivered_df=delivered_df,
            run_date=run_date,
            rng=rng,
        )

        new_pricing_df, old_pricing_ids = dim_module.generate_price_changes(
            products_df=products_df,
            run_date=run_date,
            rng=rng,
        )
        new_products_df, new_prod_pricing_df = dim_module.generate_product_launches(
            products_df=products_df,
            run_date=run_date,
            rng=rng,
        )
        new_assignments_df, rep_ids_to_close = dim_module.generate_rep_reassignments(
            reps_df=reps_df,
            run_date=run_date,
            rng=rng,
        )

        # ── 5. Assemble batch for failure injection ───────────────────────────
        batch = {
            "orders":                orders_df,
            "order_lines":           order_lines_df,
            "new_customers":         new_customers_df,
            "new_addresses":         new_addresses_df,
            "customer_updates":      customer_updates_df,
            "_existing_customer_ids": all_cust_ids,
        }
        batch = fi.inject(failure_type, batch, rng)

        # Pull back (possibly modified) DataFrames
        orders_df      = batch["orders"]
        order_lines_df = batch["order_lines"]
        new_customers_df    = batch["new_customers"]
        new_addresses_df    = batch["new_addresses"]
        customer_updates_df = batch["customer_updates"]

        # ── 6. Count summary ──────────────────────────────────────────────────
        counts = {
            "new_customers":        len(new_customers_df),
            "new_addresses":        len(new_addresses_df),
            "customer_updates":     len(customer_updates_df),
            "new_orders":           len(orders_df),
            "new_order_lines":      len(order_lines_df),
            "status_log_entries":   len(status_log_df),
            "orders_status_updated": len(order_updates_df),
            "price_changes":        len(new_pricing_df),
            "product_launches":     len(new_products_df),
            "rep_reassignments":    len(new_assignments_df),
        }

        _log_counts(run_date, counts)

        if dry_run:
            logger.info("DRY RUN — no data written to Azure SQL")
            return counts

        # ── 7. Write all data in a single transaction ─────────────────────────
        cursor = conn.cursor()
        cursor.fast_executemany = True

        cust_module.write_new_customers(new_customers_df, new_addresses_df, cursor)
        cust_module.write_customer_updates(customer_updates_df, cursor)
        orders_module.write_orders(orders_df, order_lines_df, cursor)
        status_module.write_status_updates(status_log_df, order_updates_df, cursor)
        dim_module.write_dimension_changes(
            new_pricing_df=new_pricing_df,
            old_pricing_product_ids=old_pricing_ids,
            new_products_df=new_products_df,
            new_product_pricing_df=new_prod_pricing_df,
            new_assignments_df=new_assignments_df,
            rep_ids_to_close=rep_ids_to_close,
            run_date=run_date,
            cursor=cursor,
        )

        # Handle dependency_violation flag: write a control record to trigger
        # ADF out-of-order execution on the next pipeline run
        if batch.get("_dependency_violation"):
            _write_dependency_violation_flag(cursor, run_date)

        conn.commit()
        logger.info("Transaction committed successfully")

        # ── 8. Inventory snapshot (separate transaction — full refresh) ───────
        inv_count = _write_inventory_snapshot(conn, products_df, run_date, rng)
        counts["inventory_snapshot_rows"] = inv_count

        return counts

    except Exception:
        conn.rollback()
        logger.exception("Generator run failed — transaction rolled back")
        raise
    finally:
        conn.close()


def _write_inventory_snapshot(
    conn,
    products_df,
    run_date: date,
    rng: np.random.Generator,
) -> int:
    """
    Full refresh daily inventory snapshot for all 4,200 products × 45 stores.

    Written in a separate transaction from the main batch because:
    - It's 189,000 rows — too large to combine with the main transaction
    - It's idempotent by snapshot_date: running it twice for the same date
      just inserts duplicate rows (handled by UNIQUE constraint in bootstrap_sql.sql)
    """
    import uuid as _uuid
    from datetime import timezone as _tz

    now = datetime.now(timezone.utc)
    stores = config.STORES
    rows = []

    for _, prod in products_df.iterrows():
        for store in stores:
            opening = int(rng.integers(0, 150))
            units_sold = int(rng.integers(0, min(opening + 1, 50)))
            units_returned = int(rng.integers(0, max(1, units_sold // 10)))
            closing = max(0, opening - units_sold + units_returned)
            reorder_point = int(rng.integers(10, 40))

            rows.append((
                str(_uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32))),
                str(prod["product_id"]),
                store["store_id"],
                run_date,
                opening,
                units_sold,
                units_returned,
                closing,
                1 if closing <= 0 else 0,
                reorder_point,
                now,
                "VELORA_PIM",
            ))

    sql = (
        "INSERT INTO velora_pim.inventory_snapshot "
        "(snapshot_id, product_id, store_id, snapshot_date, opening_stock, "
        "units_sold, units_returned, closing_stock, stockout_flag, reorder_point, "
        "created_at, source_system) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    # Chunked write with per-chunk commits + progress logs.
    # On Flex Consumption, the function host has been observed to silently
    # kill the Python worker mid-`executemany` for the full 189K-row insert,
    # losing all inventory rows for the day even though main-batch tables
    # had committed (S11 incident, 2026-05-11). Splitting into committed
    # chunks of 10K rows means: (a) progress is durable on partial failure,
    # (b) we get a log line every chunk so the failure point is visible in
    # App Insights, (c) each individual `executemany` is short enough that
    # any host-side timeout is unlikely to fire mid-call.
    logger.info(
        "Inventory snapshot: starting write of %d rows for date %s "
        "(chunk size = %d)",
        len(rows), run_date, 10_000,
    )
    conn.autocommit = False
    written = 0
    try:
        chunk_size = 10_000
        for i in range(0, len(rows), chunk_size):
            cursor = conn.cursor()
            cursor.fast_executemany = True
            sub_batch = config.SQL_INSERT_BATCH_SIZE
            for j in range(0, len(rows[i: i + chunk_size]), sub_batch):
                cursor.executemany(
                    sql,
                    rows[i + j: i + j + sub_batch],
                )
            conn.commit()
            written += min(chunk_size, len(rows) - i)
            logger.info(
                "Inventory snapshot: committed %d / %d rows for %s",
                written, len(rows), run_date,
            )
    except Exception:
        conn.rollback()
        logger.exception(
            "Inventory snapshot failed after %d / %d rows committed for %s",
            written, len(rows), run_date,
        )
        raise
    finally:
        conn.autocommit = True

    logger.info("Inventory snapshot: completed %d rows for date %s", len(rows), run_date)
    return len(rows)


def _write_dependency_violation_flag(cursor, run_date: date) -> None:
    """
    Insert a control record that ADF reads to trigger out-of-order Gold execution.
    Used only during dependency_violation failure injection.
    """
    cursor.execute(
        "INSERT INTO velora_oms.control_flags (flag_name, flag_value, set_at) "
        "VALUES ('force_early_fact_run', '1', ?)",
        [datetime.now(timezone.utc)],
    )
    logger.warning("dependency_violation: control flag written to force early fact run")


def _log_counts(run_date: date, counts: Dict) -> None:
    logger.info(
        "Run summary for %s: "
        "%d new customers, %d SCD updates, "
        "%d orders, %d order_lines, "
        "%d status transitions, "
        "%d price changes, %d launches, %d rep reassignments",
        run_date,
        counts["new_customers"],
        counts["customer_updates"],
        counts["new_orders"],
        counts["new_order_lines"],
        counts["status_log_entries"],
        counts["price_changes"],
        counts["product_launches"],
        counts["rep_reassignments"],
    )


# ── Azure Function entry point ────────────────────────────────────────────────


def yesterday_utc() -> date:
    """The date one day before today, in UTC.

    Reflects the canonical "nightly batch" pattern: the Function fires at
    06:00 UTC daily and writes a complete day of OLTP activity for the day
    that just ended. A 06:00 UTC fire on May 7 writes order_date = May 6 —
    Velora's full May 6 OLTP activity, ready for ADF to extract into
    landing/. The idempotency guard in run() makes this safe against
    accidental re-fires (manual + scheduled overlap, retries, etc.).
    """
    from datetime import timedelta
    today_utc = datetime.now(timezone.utc).date()
    return today_utc - timedelta(days=1)


def main(mytimer=None) -> None:
    """Azure Functions timer trigger entry point.

    Writes yesterday's full-day OLTP activity (today_utc - 1). The
    idempotency guard in run() skips cleanly if that date is already
    populated, so this is safe to fire at any cadence. ADF (Tier 6)
    handles everything downstream (landing extract, Bronze/Silver/Gold
    notebooks). This Function only simulates the source-system OLTP
    writes.
    """
    try:
        run_date = yesterday_utc()
        logger.info("Azure Function fire — writing for %s", run_date)
        counts = run(run_date=run_date)
        logger.info("Azure Function completed for %s: %s", run_date, counts)
    except Exception as exc:
        logger.exception("Azure Function failed: %s", exc)
        raise


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Velora synthetic data generator"
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="Run date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate data and validate but do not write to Azure SQL",
    )
    parser.add_argument(
        "--failure",
        choices=fi.ALL_FAILURE_TYPES,
        default=None,
        help="Inject a specific failure scenario",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=config.RANDOM_SEED,
        help=f"Random seed (default: {config.RANDOM_SEED})",
    )
    args = parser.parse_args()

    result = run(
        run_date=args.date,
        dry_run=args.dry_run,
        failure_type=args.failure,
        seed=args.seed,
    )

    print("\nRun complete:")
    for key, val in result.items():
        print(f"  {key}: {val:,}")
