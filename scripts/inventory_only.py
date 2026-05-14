"""
Run only the inventory_snapshot write for a given date.

Bypasses the generator's idempotency guard (which checks orders, not
inventory). Used to complete a partial fire where orders/lines/status_log
committed but inventory_snapshot didn't (S11 incident: function host
silently killed the worker after the main commit).

Usage:
  AZURE_SQL_PASSWORD=... .venv/bin/python scripts/inventory_only.py --date 2026-05-10

Idempotency: this script will refuse to write if inventory_snapshot already
has rows for the given date — pass --force to wipe + re-write.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "generator"))

import numpy as np
import pyodbc

import config
import catalogue as cat_module
import main as gen_main

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("inventory_only")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=lambda s: date.fromisoformat(s), required=True)
    p.add_argument("--force", action="store_true", help="Wipe existing rows for the date first")
    p.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    args = p.parse_args()

    effective_seed = args.seed + args.date.toordinal()
    rng = np.random.default_rng(effective_seed)

    if config.SQL_AUTH_MODE == "aad":
        log.info("Connecting via DefaultAzureCredential (AAD token)")
        conn = config.connect_aad(timeout=120)
    else:
        log.info("Connecting via config.get_connection_string() (mode=%s)", config.SQL_AUTH_MODE)
        conn = pyodbc.connect(config.get_connection_string(), timeout=90)

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM velora_pim.inventory_snapshot WHERE snapshot_date = ?",
        args.date,
    )
    existing = cur.fetchone()[0]
    if existing > 0 and not args.force:
        log.warning("inventory_snapshot already has %d rows for %s — skipping (use --force to wipe+rewrite)", existing, args.date)
        return
    if existing > 0 and args.force:
        log.warning("Wiping %d existing inventory rows for %s", existing, args.date)
        cur.execute("DELETE FROM velora_pim.inventory_snapshot WHERE snapshot_date = ?", args.date)
        conn.commit()

    products_df = cat_module.load_from_db(conn)["products"]
    log.info("Loaded %d products from catalogue", len(products_df))

    n = gen_main._write_inventory_snapshot(conn, products_df, args.date, rng)
    log.info("Wrote %d inventory rows for %s", n, args.date)
    conn.close()


if __name__ == "__main__":
    main()
