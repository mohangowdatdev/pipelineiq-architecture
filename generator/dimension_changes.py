"""
Slowly-changing dimension events for Velora master data.

Runs three types of changes based on the calendar:
  - Product price changes   : every Monday, 3-8 products
  - New product launches    : 1st of each month, 5-10 new SKUs
  - Rep territory changes   : 1st day of each quarter, 1-2 reassignments

Each change touches the Azure SQL source tables. Downstream Gold notebooks
detect these events and apply SCD Type 2 logic (new surrogate keys, close
old rows, open new rows with updated valid_from/valid_to).
"""

import uuid
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
from catalogue import _uid, _make_product_name, _make_sku

logger = logging.getLogger(__name__)

_QUARTER_START_MONTHS = {1, 4, 7, 10}


def generate_price_changes(
    products_df: pd.DataFrame,
    run_date: date,
    rng: np.random.Generator,
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Generate new pricing records and return IDs of old records to close.

    Only runs on Mondays. Returns (new_pricing_df, old_pricing_ids_to_close).
    Returns empty structures on non-Monday runs.
    """
    if run_date.weekday() != 0:  # 0 = Monday
        return pd.DataFrame(), []

    now = datetime.now(timezone.utc)
    yesterday = run_date - timedelta(days=1)
    n_changes = int(rng.integers(3, 9))  # 3-8 products

    sample = products_df.sample(
        n=min(n_changes, len(products_df)),
        random_state=int(rng.integers(0, 10_000)),
    )

    new_pricing_rows = []
    pricing_ids_to_close = []

    for _, prod in sample.iterrows():
        division = prod["division"]
        price_min, price_max = config.DIVISION_PRICE_RANGES[division]
        new_list_price = round(float(rng.uniform(price_min, price_max)), 2)
        cost_ratio = float(rng.uniform(
            config.COST_PRICE_RATIO_MIN, config.COST_PRICE_RATIO_MAX
        ))
        new_cost_price = round(new_list_price * cost_ratio, 2)

        new_pricing_id = str(uuid.UUID(
            int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)
        ))

        new_pricing_rows.append({
            "pricing_id":     new_pricing_id,
            "product_id":     prod["product_id"],
            "list_price":     new_list_price,
            "cost_price":     new_cost_price,
            "effective_from": run_date,
            "effective_to":   None,
            "pricing_type":   "STANDARD",
            "created_at":     now,
            "source_system":  "VELORA_PIM",
        })

        # Close the old active pricing record
        pricing_ids_to_close.append(str(prod["product_id"]))

    logger.info("Generated %d product price changes", len(new_pricing_rows))
    return pd.DataFrame(new_pricing_rows), pricing_ids_to_close


def generate_product_launches(
    products_df: pd.DataFrame,
    run_date: date,
    rng: np.random.Generator,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate new product SKUs and initial pricing.

    Only runs on the 1st of each month. Returns (new_products_df, new_pricing_df).
    """
    if run_date.day != 1:
        return pd.DataFrame(), pd.DataFrame()

    now = datetime.now(timezone.utc)
    n_launches = int(rng.integers(5, 11))  # 5-10 new SKUs

    # Determine the next available sequence number
    current_max_seq = len(products_df) + 1

    new_product_rows = []
    new_pricing_rows = []

    for i in range(n_launches):
        division = str(rng.choice(config.DIVISIONS))
        cat_ids = config.CATEGORY_IDS_BY_DIVISION[division]
        category_id = cat_ids[int(rng.integers(0, len(cat_ids)))]
        brands = config.DIVISION_BRANDS[division]
        brand = brands[int(rng.integers(0, len(brands)))]

        seq = current_max_seq + i
        sku = _make_sku(division, category_id, seq)
        product_id = str(uuid.UUID(
            int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)
        ))
        product_name = _make_product_name(division, brand, seq, rng)

        new_product_rows.append({
            "product_id":    product_id,
            "sku":           sku,
            "product_name":  product_name,
            "category_id":   category_id,
            "division":      division,
            "brand":         brand,
            "is_active":     True,
            "launched_date": run_date,
            "created_at":    now,
            "updated_at":    now,
            "source_system": "VELORA_PIM",
        })

        price_min, price_max = config.DIVISION_PRICE_RANGES[division]
        list_price = round(float(rng.uniform(price_min, price_max)), 2)
        cost_ratio = float(rng.uniform(
            config.COST_PRICE_RATIO_MIN, config.COST_PRICE_RATIO_MAX
        ))
        cost_price = round(list_price * cost_ratio, 2)
        pricing_id = str(uuid.UUID(
            int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)
        ))

        new_pricing_rows.append({
            "pricing_id":     pricing_id,
            "product_id":     product_id,
            "list_price":     list_price,
            "cost_price":     cost_price,
            "effective_from": run_date,
            "effective_to":   None,
            "pricing_type":   "STANDARD",
            "created_at":     now,
            "source_system":  "VELORA_PIM",
        })

    logger.info("Generated %d new product launches", n_launches)
    return pd.DataFrame(new_product_rows), pd.DataFrame(new_pricing_rows)


def generate_rep_reassignments(
    reps_df: pd.DataFrame,
    run_date: date,
    rng: np.random.Generator,
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Generate sales rep territory reassignments.

    Only runs on the 1st day of each quarter (Jan 1, Apr 1, Jul 1, Oct 1).
    Returns (new_assignments_df, rep_ids_with_closed_assignment).
    """
    if not (run_date.day == 1 and run_date.month in _QUARTER_START_MONTHS):
        return pd.DataFrame(), []

    now = datetime.now(timezone.utc)
    n_changes = int(rng.integers(1, 3))  # 1-2 reassignments

    sample = reps_df.sample(
        n=min(n_changes, len(reps_df)),
        random_state=int(rng.integers(0, 10_000)),
    )

    territories = [c["territory_id"] for c in config.CITIES]
    new_assignment_rows = []
    rep_ids_to_close = []

    for _, rep in sample.iterrows():
        current_territory = rep.get("territory_id", "TER-001")
        available = [t for t in territories if t != current_territory]
        new_territory = available[int(rng.integers(0, len(available)))]

        assignment_id = str(uuid.UUID(
            int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)
        ))
        new_assignment_rows.append({
            "assignment_id":  assignment_id,
            "rep_id":         rep["rep_id"],
            "territory_id":   new_territory,
            "assigned_from":  run_date,
            "assigned_to":    None,
            "is_current":     True,
            "created_at":     now,
            "source_system":  "VELORA_HRM",
        })
        rep_ids_to_close.append(str(rep["rep_id"]))

    logger.info("Generated %d rep territory reassignments", len(new_assignment_rows))
    return pd.DataFrame(new_assignment_rows), rep_ids_to_close


def write_dimension_changes(
    new_pricing_df: pd.DataFrame,
    old_pricing_product_ids: List[str],
    new_products_df: pd.DataFrame,
    new_product_pricing_df: pd.DataFrame,
    new_assignments_df: pd.DataFrame,
    rep_ids_to_close: List[str],
    run_date: date,
    cursor,
) -> None:
    """Write all dimension changes to Azure SQL in the caller's transaction."""
    yesterday = run_date - timedelta(days=1)

    # Close old pricing records for products that got new prices
    if old_pricing_product_ids:
        placeholders = ", ".join(["?"] * len(old_pricing_product_ids))
        cursor.execute(
            f"UPDATE velora_pim.product_pricing "
            f"SET effective_to = ? "
            f"WHERE product_id IN ({placeholders}) AND effective_to IS NULL",
            [yesterday] + old_pricing_product_ids,
        )

    # Insert new pricing records
    _insert_pricing(new_pricing_df, cursor)

    # Insert new products and their pricing
    _insert_new_products(new_products_df, cursor)
    _insert_pricing(new_product_pricing_df, cursor)

    # Close old territory assignments for reassigned reps
    if rep_ids_to_close:
        placeholders = ", ".join(["?"] * len(rep_ids_to_close))
        cursor.execute(
            f"UPDATE velora_hrm.territory_assignments "
            f"SET assigned_to = ?, is_current = 0 "
            f"WHERE rep_id IN ({placeholders}) AND is_current = 1",
            [yesterday] + rep_ids_to_close,
        )

    # Insert new territory assignments
    _insert_assignments(new_assignments_df, cursor)

    logger.info(
        "Dimension changes written: %d price changes, %d launches, %d reassignments",
        len(new_pricing_df), len(new_products_df), len(new_assignments_df),
    )


def _insert_pricing(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["pricing_id", "product_id", "list_price", "cost_price",
            "effective_from", "effective_to", "pricing_type",
            "created_at", "source_system"]
    sql = (f"INSERT INTO velora_pim.product_pricing ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [
        tuple(None if v is None else v for v in (row[c] for c in cols))
        for _, row in df.iterrows()
    ]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])


def _insert_new_products(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["product_id", "sku", "product_name", "category_id", "division",
            "brand", "is_active", "launched_date",
            "created_at", "updated_at", "source_system"]
    sql = (f"INSERT INTO velora_pim.products ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])


def _insert_assignments(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["assignment_id", "rep_id", "territory_id",
            "assigned_from", "assigned_to", "is_current",
            "created_at", "source_system"]
    sql = (f"INSERT INTO velora_hrm.territory_assignments ({', '.join(cols)}) "
           f"VALUES ({', '.join(['?']*len(cols))})")
    rows = [
        tuple(None if v is None else v for v in (row[c] for c in cols))
        for _, row in df.iterrows()
    ]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])
