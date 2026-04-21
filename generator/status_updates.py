"""
Order status progression for velora_oms.

Loads open orders from Azure SQL and advances their status based on order age.
Each transition writes to order_status_log and updates the order's status field.

Transition rules (from config.STATUS_PROGRESSION):
  PENDING    → PROCESSING  (age >= 1 day)
  PROCESSING → SHIPPED     (age >= 3 days)
  SHIPPED    → DELIVERED   (age >= 7 days)
  DELIVERED  → RETURN_INITIATED (2% of delivered orders — random selection)
"""

import uuid
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"DELIVERED", "CANCELLED", "RETURNED", "RETURN_INITIATED"}


def load_open_orders(conn, run_date: date) -> pd.DataFrame:
    """Load orders in non-terminal states with their creation dates."""
    status_list = ", ".join(f"'{s}'" for s in
                             {"PENDING", "PROCESSING", "SHIPPED"})
    return pd.read_sql(
        f"SELECT order_id, status, order_date, created_at "
        f"FROM velora_oms.orders "
        f"WHERE status IN ({status_list})",
        conn,
    )


def load_recently_delivered_orders(conn, run_date: date) -> pd.DataFrame:
    """Load DELIVERED orders from the past 7 days (candidates for return initiation)."""
    cutoff = run_date - timedelta(days=7)
    return pd.read_sql(
        f"SELECT order_id, status, order_date "
        f"FROM velora_oms.orders "
        f"WHERE status = 'DELIVERED' AND order_date >= '{cutoff}'",
        conn,
    )


def generate_status_updates(
    open_orders_df: pd.DataFrame,
    delivered_df: pd.DataFrame,
    run_date: date,
    rng: np.random.Generator,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Compute status transitions for open orders.

    Returns (status_log_df, orders_to_update_df).
      status_log_df     — new rows for velora_oms.order_status_log
      orders_to_update_df — (order_id, new_status) pairs for UPDATE
    """
    now = datetime.now(timezone.utc)
    log_rows: List[Dict] = []
    update_rows: List[Dict] = []

    # Advance open orders through the lifecycle
    for _, row in open_orders_df.iterrows():
        current_status = row["status"]
        if current_status not in config.STATUS_PROGRESSION:
            continue

        rule = config.STATUS_PROGRESSION[current_status]
        order_age = (run_date - pd.Timestamp(row["order_date"]).date()).days

        if order_age < rule["min_age_days"]:
            continue

        new_status = rule["target"]
        log_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))

        log_rows.append({
            "log_id":        log_id,
            "order_id":      row["order_id"],
            "from_status":   current_status,
            "to_status":     new_status,
            "changed_at":    now,
            "changed_by":    "SYSTEM",
            "created_at":    now,
            "source_system": "VELORA_OMS",
        })
        update_rows.append({
            "order_id":   row["order_id"],
            "new_status": new_status,
            "updated_at": now,
        })

    # Return initiations on a random sample of recently delivered orders
    if not delivered_df.empty:
        n_returns = max(0, int(len(delivered_df) * config.RETURN_INITIATION_RATE))
        if n_returns > 0:
            candidates = delivered_df.sample(
                n=min(n_returns, len(delivered_df)),
                random_state=int(rng.integers(0, 10_000)),
            )
            for _, row in candidates.iterrows():
                log_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))
                log_rows.append({
                    "log_id":        log_id,
                    "order_id":      row["order_id"],
                    "from_status":   "DELIVERED",
                    "to_status":     "RETURN_INITIATED",
                    "changed_at":    now,
                    "changed_by":    "SYSTEM",
                    "created_at":    now,
                    "source_system": "VELORA_OMS",
                })
                update_rows.append({
                    "order_id":   row["order_id"],
                    "new_status": "RETURN_INITIATED",
                    "updated_at": now,
                })

    logger.info("Generated %d status transitions", len(log_rows))

    return pd.DataFrame(log_rows), pd.DataFrame(update_rows)


def write_status_updates(
    log_df: pd.DataFrame,
    updates_df: pd.DataFrame,
    cursor,
) -> None:
    """INSERT status log records and UPDATE order statuses."""
    _insert_status_log(log_df, cursor)
    _update_order_statuses(updates_df, cursor)
    logger.info(
        "Wrote %d status log entries, updated %d orders",
        len(log_df), len(updates_df),
    )


def _insert_status_log(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["log_id", "order_id", "from_status", "to_status",
            "changed_at", "changed_by", "created_at", "source_system"]
    sql = (f"INSERT INTO velora_oms.order_status_log ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])


def _update_order_statuses(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    sql = "UPDATE velora_oms.orders SET status = ?, updated_at = ? WHERE order_id = ?"
    rows = [(r["new_status"], r["updated_at"], r["order_id"])
            for _, r in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])
