"""
Controlled failure injection for PipelineIQ observability testing.

Each failure class exercises a specific detection path in the pipeline.
Pass the failure_type to run() in main.py to inject bad data into the batch
before it is written to Azure SQL.

Failure classes:
  schema_drift           — adds promo_code column to orders (breaks Silver MERGE)
  referential_integrity  — 30 order_lines with product_id=9999 (quarantined by Silver DQ)
  volume_anomaly         — reduces order count to 12 (pipeline succeeds, volume alert fires)
  null_constraint        — 50 order_lines with null unit_price (rejected by Silver DQ)
  scd_key_explosion      — 800 customer updates in one batch (Gold latency spike)
  dependency_violation   — sets control flag that ADF reads to trigger out-of-order run

When failure_type is None, the batch is returned unmodified.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_DRIFT          = "schema_drift"
REFERENTIAL_INTEGRITY = "referential_integrity"
VOLUME_ANOMALY        = "volume_anomaly"
NULL_CONSTRAINT       = "null_constraint"
SCD_KEY_EXPLOSION     = "scd_key_explosion"
DEPENDENCY_VIOLATION  = "dependency_violation"

ALL_FAILURE_TYPES = [
    SCHEMA_DRIFT,
    REFERENTIAL_INTEGRITY,
    VOLUME_ANOMALY,
    NULL_CONSTRAINT,
    SCD_KEY_EXPLOSION,
    DEPENDENCY_VIOLATION,
]

# Sentinel product_id used by referential_integrity scenario.
# This ID does not exist in velora_pim.products.
_GHOST_PRODUCT_ID = "00000000-0000-0000-0000-000000009999"


def inject(
    failure_type: Optional[str],
    batch: Dict[str, pd.DataFrame],
    rng: np.random.Generator,
) -> Dict[str, pd.DataFrame]:
    """
    Apply the specified failure to the data batch.

    Args:
        failure_type: one of the ALL_FAILURE_TYPES constants, or None for clean run
        batch: dict with keys matching table names:
               orders, order_lines, new_customers, customer_updates
        rng: seeded random generator (mutated in place for reproducibility)

    Returns:
        Modified batch dict. Original DataFrames may be replaced, not mutated.
    """
    if failure_type is None:
        return batch

    if failure_type not in ALL_FAILURE_TYPES:
        raise ValueError(
            f"Unknown failure_type '{failure_type}'. "
            f"Valid types: {ALL_FAILURE_TYPES}"
        )

    logger.warning("FAILURE INJECTION ACTIVE: %s", failure_type)

    if failure_type == SCHEMA_DRIFT:
        return _inject_schema_drift(batch, rng)

    if failure_type == REFERENTIAL_INTEGRITY:
        return _inject_referential_integrity(batch, rng)

    if failure_type == VOLUME_ANOMALY:
        return _inject_volume_anomaly(batch, rng)

    if failure_type == NULL_CONSTRAINT:
        return _inject_null_constraint(batch, rng)

    if failure_type == SCD_KEY_EXPLOSION:
        return _inject_scd_key_explosion(batch, rng)

    if failure_type == DEPENDENCY_VIOLATION:
        return _inject_dependency_violation(batch, rng)

    return batch


def _inject_schema_drift(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Add promo_code column to orders DataFrame.

    Effect downstream:
      Bronze: loads fine (schema-on-read Parquet — extra column is just null elsewhere)
      Silver: MERGE fails because orders table definition does not include promo_code
      RCA target: "New source column detected. Enable schema evolution or add
                   column explicitly to Silver schema."
    """
    orders = batch.get("orders", pd.DataFrame()).copy()
    if orders.empty:
        logger.warning("schema_drift: orders DataFrame is empty, nothing to inject")
        return batch

    promo_codes = ["DIWALI20", "SAVE15", "NEWUSER10", None, "FLASH25", "VIP30"]
    orders["promo_code"] = [
        promo_codes[int(rng.integers(0, len(promo_codes)))]
        for _ in range(len(orders))
    ]

    logger.info(
        "schema_drift injected: added promo_code column to %d orders", len(orders)
    )
    return {**batch, "orders": orders}


def _inject_referential_integrity(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Replace product_id in 30 order_lines with a non-existent product ID (9999).

    Effect downstream:
      Silver DQ: checks product_id existence, finds 9999 is unknown
      Outcome: 30 lines quarantined with rejection_reason = UNKNOWN_PRODUCT_ID
      RCA target: "Orphaned FK on product_id. Product master not synced for
                   new SKUs introduced today."
    """
    lines = batch.get("order_lines", pd.DataFrame()).copy()
    if lines.empty or len(lines) < 30:
        logger.warning(
            "referential_integrity: fewer than 30 order_lines (%d), injecting all",
            len(lines),
        )
        n = len(lines)
    else:
        n = 30

    indices = rng.choice(len(lines), size=n, replace=False)
    lines.iloc[indices, lines.columns.get_loc("product_id")] = _GHOST_PRODUCT_ID

    logger.info(
        "referential_integrity injected: %d order_lines now reference product_id=%s",
        n, _GHOST_PRODUCT_ID,
    )
    return {**batch, "order_lines": lines}


def _inject_volume_anomaly(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Truncate orders to only 12 (simulates POS system outage).

    Effect downstream:
      All notebooks succeed — no pipeline error
      Observability: row count is 96% below 7-day rolling average
      Detection: volume anomaly alert fires in PipelineIQ
      RCA target: "Volume alert fired despite successful pipeline run.
                   Silent data loss — invisible to traditional monitoring."
    """
    orders = batch.get("orders", pd.DataFrame())
    lines = batch.get("order_lines", pd.DataFrame())
    original_count = len(orders)

    if orders.empty:
        return batch

    # Keep only 12 orders (or fewer if we don't have that many)
    target = min(12, len(orders))
    kept_orders = orders.head(target)
    kept_order_ids = set(kept_orders["order_id"].tolist())
    kept_lines = lines[lines["order_id"].isin(kept_order_ids)]

    logger.info(
        "volume_anomaly injected: reduced orders from %d to %d (dropped %d)",
        original_count, len(kept_orders), original_count - len(kept_orders),
    )
    return {**batch, "orders": kept_orders, "order_lines": kept_lines}


def _inject_null_constraint(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Set unit_price to None in 50 order_lines.

    Effect downstream:
      Silver DQ: unit_price must not be null
      Outcome: 50 lines quarantined with rejection_reason = NULL_REQUIRED_FIELD
      RCA target: "Null in non-nullable price column. Pricing lookup returned
                   no match for SKUs launched today."
    """
    lines = batch.get("order_lines", pd.DataFrame()).copy()
    if lines.empty:
        return batch

    n = min(50, len(lines))
    indices = rng.choice(len(lines), size=n, replace=False)
    lines.iloc[indices, lines.columns.get_loc("unit_price")] = None

    logger.info(
        "null_constraint injected: %d order_lines have null unit_price", n
    )
    return {**batch, "order_lines": lines}


def _inject_scd_key_explosion(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Generate 800 customer update events in a single batch.

    Effect downstream:
      Gold dim_customer: 800 new SCD Type 2 rows in one run
      Latency: Gold job run time spikes from ~8 minutes to ~45 minutes
      No notebook failure — the data is valid, just voluminous
      RCA target: "Unusual SCD Type 2 volume caused Gold latency spike.
                   Investigate upstream CRM bulk migration job."
    """
    existing_ids = batch.get("_existing_customer_ids", [])
    if not existing_ids:
        logger.warning("scd_key_explosion: no existing customer IDs available to update")
        return batch

    now = datetime.now(timezone.utc)
    n = min(800, len(existing_ids))
    indices = rng.choice(len(existing_ids), size=n, replace=False)
    sampled_ids = [existing_ids[i] for i in indices]

    cities = __import__("config").CITIES
    explosion_updates = []
    for cid in sampled_ids:
        city_info = cities[int(rng.integers(0, len(cities)))]
        explosion_updates.append({
            "customer_id": cid,
            "segment":     "VIP",
            "city":        city_info["city"],
            "state":       city_info["state"],
            "updated_at":  now,
        })

    logger.info(
        "scd_key_explosion injected: %d customer update events (normal is 5-15)", n
    )
    return {**batch, "customer_updates": pd.DataFrame(explosion_updates)}


def _inject_dependency_violation(batch: Dict, rng: np.random.Generator) -> Dict:
    """
    Set a control flag in the batch metadata to trigger ADF out-of-order execution.

    In the ADF pipeline, the Gold fact pipeline checks a 'force_early_fact_run'
    flag in PostgreSQL process_queue. This injection sets that flag so ADF
    triggers fact_order_line before dim_product has finished refreshing.

    Effect downstream:
      Gold: FK lookups on product_surrogate_key return NULL for all products
            (dim_product not yet refreshed when fact pipeline runs)
      RCA target: "Gold fact loaded before dimension refresh completed.
                   Dependency ordering violated in ADF pipeline."
    """
    batch["_dependency_violation"] = True
    logger.info(
        "dependency_violation injected: _dependency_violation flag set in batch metadata"
    )
    return batch


def describe_failure(failure_type: str) -> str:
    """Return a one-line description of what the failure injects."""
    descriptions = {
        SCHEMA_DRIFT:          "Adds promo_code column to orders — breaks Silver MERGE schema check",
        REFERENTIAL_INTEGRITY: "Sets product_id=9999 on 30 order_lines — quarantined by Silver DQ",
        VOLUME_ANOMALY:        "Truncates orders to 12 — pipeline succeeds but volume alert fires",
        NULL_CONSTRAINT:       "Sets unit_price=NULL on 50 order_lines — rejected by Silver DQ",
        SCD_KEY_EXPLOSION:     "Generates 800 customer updates — Gold latency spike from ~8min to ~45min",
        DEPENDENCY_VIOLATION:  "Sets flag for ADF out-of-order execution — null surrogate keys in Gold",
    }
    return descriptions.get(failure_type, "Unknown failure type")
