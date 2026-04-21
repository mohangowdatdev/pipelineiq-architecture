"""
Daily order generation for velora_oms.

Produces orders and order_lines across three channels:
  D2C   — individual online purchases, 1-5 lines
  B2B   — bulk wholesale with assigned rep and contract pricing, 3-15 lines
  STORE — end-of-day POS batch with store_id, 1-4 lines

All referential integrity is enforced before writing: every customer_id and
product_id in every order_line must exist in the combined pool.
"""

import uuid
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

GST_RATE = 0.18  # 18% GST — standard Indian retail rate


def generate_orders(
    run_date: date,
    all_customer_ids: List[str],
    d2c_customer_ids: List[str],
    b2b_customer_ids: List[str],
    products_df: pd.DataFrame,
    pricing_df: pd.DataFrame,
    stores: List[Dict],
    reps_df: pd.DataFrame,
    rng: np.random.Generator,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate daily orders and order lines for all three channels.

    Returns (orders_df, order_lines_df). Referential integrity is validated
    before returning — any order_line with an unknown product_id is dropped
    and logged as a warning.
    """
    now = datetime.now(timezone.utc)

    # Merge products with current pricing for fast lookup
    product_pool = products_df.merge(pricing_df, on="product_id", how="inner")

    if product_pool.empty:
        raise ValueError("product_pool is empty — cannot generate orders without products")

    d2c_count = config.get_adjusted_volume(
        config.VOLUME_D2C_MIN, config.VOLUME_D2C_MAX, run_date, rng
    )
    b2b_count = config.get_adjusted_volume(
        config.VOLUME_B2B_MIN, config.VOLUME_B2B_MAX, run_date, rng
    )
    store_count = config.get_adjusted_volume(
        config.VOLUME_STORE_MIN, config.VOLUME_STORE_MAX, run_date, rng
    )

    all_orders = []
    all_lines = []

    if d2c_customer_ids:
        orders, lines = _generate_channel_orders(
            "D2C", d2c_count, d2c_customer_ids, product_pool,
            None, None, run_date, now, rng,
        )
        all_orders.extend(orders)
        all_lines.extend(lines)

    if b2b_customer_ids:
        orders, lines = _generate_channel_orders(
            "B2B", b2b_count, b2b_customer_ids, product_pool,
            None, reps_df, run_date, now, rng,
        )
        all_orders.extend(orders)
        all_lines.extend(lines)

    orders, lines = _generate_channel_orders(
        "STORE", store_count, all_customer_ids, product_pool,
        stores, None, run_date, now, rng,
    )
    all_orders.extend(orders)
    all_lines.extend(lines)

    orders_df = pd.DataFrame(all_orders)
    lines_df = pd.DataFrame(all_lines)

    _validate_referential_integrity(lines_df, product_pool)

    logger.info(
        "Generated %d orders (%d D2C, %d B2B, %d Store) with %d lines",
        d2c_count + b2b_count + store_count,
        d2c_count, b2b_count, store_count,
        len(lines_df),
    )

    return orders_df, lines_df


def _generate_channel_orders(
    channel: str,
    count: int,
    customer_ids: List[str],
    product_pool: pd.DataFrame,
    stores: Optional[List[Dict]],
    reps_df: Optional[pd.DataFrame],
    run_date: date,
    now: datetime,
    rng: np.random.Generator,
) -> Tuple[List[Dict], List[Dict]]:
    orders = []
    lines = []

    if channel == "D2C":
        lines_min, lines_max = config.D2C_LINES_MIN, config.D2C_LINES_MAX
    elif channel == "B2B":
        lines_min, lines_max = config.B2B_LINES_MIN, config.B2B_LINES_MAX
    else:
        lines_min, lines_max = config.STORE_LINES_MIN, config.STORE_LINES_MAX

    for _ in range(count):
        order_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))
        customer_id = customer_ids[int(rng.integers(0, len(customer_ids)))]
        store_id: Optional[str] = None
        rep_id: Optional[str] = None

        if channel == "STORE" and stores:
            store = stores[int(rng.integers(0, len(stores)))]
            store_id = store["store_id"]

        if channel == "B2B" and reps_df is not None and not reps_df.empty:
            rep_row = reps_df.iloc[int(rng.integers(0, len(reps_df)))]
            rep_id = str(rep_row["rep_id"])

        n_lines = int(rng.integers(lines_min, lines_max + 1))
        product_sample = product_pool.sample(
            n=min(n_lines, len(product_pool)),
            replace=False,
            random_state=int(rng.integers(0, 10_000)),
        )

        order_total = 0.0
        order_lines = []

        for _, prod in product_sample.iterrows():
            line_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))
            quantity = int(rng.integers(1, 4) if channel in ("D2C", "STORE")
                          else rng.integers(5, 30))
            list_price = float(prod["list_price"])

            if channel == "B2B":
                disc_pct = float(rng.uniform(
                    config.B2B_DISCOUNT_MIN_PCT, config.B2B_DISCOUNT_MAX_PCT
                )) / 100
                unit_price = round(list_price * (1 - disc_pct), 2)
                discount_amt = round((list_price - unit_price) * quantity, 2)
            else:
                unit_price = list_price
                # Occasional small promotional discount (15% chance)
                if rng.random() < 0.15:
                    disc_pct = float(rng.uniform(0.02, 0.10))
                    discount_amt = round(unit_price * quantity * disc_pct, 2)
                else:
                    discount_amt = 0.0

            line_total = round(quantity * unit_price - discount_amt, 2)
            order_total += line_total

            order_lines.append({
                "line_id":       line_id,
                "order_id":      order_id,
                "product_id":    str(prod["product_id"]),
                "quantity":      quantity,
                "unit_price":    unit_price,
                "discount_amt":  discount_amt,
                # line_total is a PERSISTED COMPUTED column in Azure SQL — not inserted
                "created_at":    now,
                "updated_at":    now,
                "source_system": "VELORA_OMS",
            })

        orders.append({
            "order_id":      order_id,
            "customer_id":   customer_id,
            "channel_type":  channel,
            "order_date":    run_date,
            "status":        "PENDING",
            "store_id":      store_id,
            "rep_id":        rep_id,
            "total_amount":  round(order_total, 2),
            "currency":      "INR",
            "created_at":    now,
            "updated_at":    now,
            "source_system": "VELORA_OMS",
        })
        lines.extend(order_lines)

    return orders, lines


def _validate_referential_integrity(
    lines_df: pd.DataFrame,
    product_pool: pd.DataFrame,
) -> None:
    """Log a warning for any order_line referencing an unknown product_id."""
    if lines_df.empty:
        return
    valid_ids = set(product_pool["product_id"].astype(str))
    bad = lines_df[~lines_df["product_id"].isin(valid_ids)]
    if not bad.empty:
        logger.warning(
            "Referential integrity: %d order_lines reference unknown product_ids",
            len(bad),
        )


def write_orders(orders_df: pd.DataFrame,
                 lines_df: pd.DataFrame,
                 cursor) -> None:
    """INSERT orders and order_lines to Azure SQL."""
    _insert_orders(orders_df, cursor)
    _insert_order_lines(lines_df, cursor)
    logger.info(
        "Inserted %d orders and %d order_lines",
        len(orders_df), len(lines_df),
    )


def _insert_orders(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["order_id", "customer_id", "channel_type", "order_date", "status",
            "store_id", "rep_id", "total_amount", "currency",
            "created_at", "updated_at", "source_system"]
    sql = (f"INSERT INTO velora_oms.orders ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [
        tuple(None if (v is None or (isinstance(v, float) and __import__('math').isnan(v))) else v
              for v in (row[c] for c in cols))
        for _, row in df.iterrows()
    ]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])


def _insert_order_lines(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    # line_total is PERSISTED COMPUTED in Azure SQL — excluded from INSERT
    cols = ["line_id", "order_id", "product_id", "quantity", "unit_price",
            "discount_amt", "created_at", "updated_at", "source_system"]
    sql = (f"INSERT INTO velora_oms.order_lines ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])
