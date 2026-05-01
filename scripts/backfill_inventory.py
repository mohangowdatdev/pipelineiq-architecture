"""
Backfill velora_pim.inventory_snapshot for one or more dates.

Used when the main generator run committed its relational batch but the
inventory snapshot transaction died mid-write (typical cause: VPN + serverless
Azure SQL TCP reset during long executemany).

Idempotent: DELETEs existing rows for the target snapshot_date before
INSERT, so partial prior writes are cleaned up on each invocation.

Resilient: wraps executemany in a retry loop that catches pyodbc
OperationalError (08S01 transient) and reconnects before retrying the chunk.

Usage:
    AZURE_SQL_* env vars set as for generator/main.py
    .venv/bin/python scripts/backfill_inventory.py 2026-01-17 2026-01-21
"""

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timezone

# Add generator/ to path so we can reuse config + catalogue loader
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "generator"))

import numpy as np
import pyodbc

import config
import catalogue as cat_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_inventory")

CHUNK_SIZE = 1000
MAX_ATTEMPTS = 5
BASE_BACKOFF = 2


def connect():
    conn = pyodbc.connect(config.get_connection_string())
    conn.autocommit = True
    return conn


def new_cursor(conn):
    cur = conn.cursor()
    cur.fast_executemany = True
    return cur


def build_rows(products_df, run_date, rng):
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
                str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32))),
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
    return rows


def delete_existing(conn, run_date):
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM velora_pim.inventory_snapshot WHERE snapshot_date = ?",
        run_date,
    )
    deleted = cur.rowcount
    if deleted > 0:
        logger.warning("Deleted %d stale rows for %s before backfill", deleted, run_date)
    return deleted


def write_chunk_with_retry(state, rows_chunk, sql, run_date):
    """
    Reuse state['conn'] + state['cursor']. On transient error, reconnect and
    retry only the failed chunk. Previous chunks are already committed
    (autocommit=True), so no data is lost.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            state["cursor"].executemany(sql, rows_chunk)
            return
        except pyodbc.OperationalError as exc:
            if attempt == MAX_ATTEMPTS:
                logger.error("chunk for %s failed after %d attempts: %s", run_date, attempt, exc)
                raise
            backoff = BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "transient error on %s chunk (attempt %d/%d): %s — reconnect + retry in %ds",
                run_date, attempt, MAX_ATTEMPTS, exc, backoff,
            )
            try:
                state["conn"].close()
            except Exception:
                pass
            time.sleep(backoff)
            state["conn"] = connect()
            state["cursor"] = new_cursor(state["conn"])


def backfill_date(run_date, products_df):
    logger.info("Backfilling inventory_snapshot for %s", run_date)
    effective_seed = config.RANDOM_SEED + run_date.toordinal()
    rng = np.random.default_rng(effective_seed)

    rows = build_rows(products_df, run_date, rng)
    logger.info("Built %d rows in memory for %s (seed=%d)", len(rows), run_date, effective_seed)

    sql = (
        "INSERT INTO velora_pim.inventory_snapshot "
        "(snapshot_id, product_id, store_id, snapshot_date, opening_stock, "
        "units_sold, units_returned, closing_stock, stockout_flag, reorder_point, "
        "created_at, source_system) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    state = {"conn": connect()}
    state["cursor"] = new_cursor(state["conn"])
    try:
        delete_existing(state["conn"], run_date)

        total = 0
        start = time.time()
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i: i + CHUNK_SIZE]
            write_chunk_with_retry(state, chunk, sql, run_date)
            total += len(chunk)
            if (i // CHUNK_SIZE) % 40 == 0 and i > 0:
                rate = total / (time.time() - start)
                logger.info("  %s: %d/%d rows written (%.0f rows/s)", run_date, total, len(rows), rate)

        logger.info("Backfill complete for %s: %d rows written in %.1fs", run_date, total, time.time() - start)
    finally:
        try:
            state["conn"].close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Backfill inventory_snapshot for given dates")
    parser.add_argument("dates", nargs="+", type=lambda s: date.fromisoformat(s))
    args = parser.parse_args()

    conn = connect()
    live_catalogue = cat_module.load_from_db(conn)
    products_df = live_catalogue["products"]
    conn.close()
    logger.info("Loaded %d products from catalogue", len(products_df))

    for run_date in args.dates:
        backfill_date(run_date, products_df)

    logger.info("All backfills complete")


if __name__ == "__main__":
    main()
