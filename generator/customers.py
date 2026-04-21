"""
Daily customer generation for velora_crm.

Generates new customer registrations (INSERTs) and customer profile updates
(UPDATEs to segment or city — triggers SCD Type 2 logic in downstream Silver).
"""

import uuid
import logging
from datetime import date, datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from faker import Faker

import config

logger = logging.getLogger(__name__)

_SEGMENT_UPGRADE_MAP = {
    "INDIVIDUAL": "VIP",
    "VIP": "VIP",
    "BUSINESS": "BUSINESS",
}

_ADDRESS_TYPES = ["HOME", "WORK", "BILLING", "SHIPPING"]
_PINCODES_BY_CITY: Dict[str, List[str]] = {
    "Bangalore":  ["560001", "560002", "560003", "560034", "560066", "560076", "560100"],
    "Chennai":    ["600001", "600002", "600006", "600020", "600041", "600083"],
    "Hyderabad":  ["500001", "500003", "500008", "500016", "500034", "500081"],
    "Mumbai":     ["400001", "400002", "400005", "400013", "400051", "400063", "400092"],
    "Pune":       ["411001", "411004", "411007", "411014", "411028", "411045"],
    "Delhi":      ["110001", "110003", "110007", "110017", "110029", "110048", "110075"],
    "Kolkata":    ["700001", "700002", "700006", "700012", "700019", "700052"],
    "Ahmedabad":  ["380001", "380004", "380006", "380009", "380015", "380058"],
}


def load_existing_customers(conn) -> pd.DataFrame:
    """Load all customer IDs and key attributes from Azure SQL."""
    return pd.read_sql(
        "SELECT customer_id, full_name, email, segment, city, state, "
        "account_type, is_active "
        "FROM velora_crm.customers",
        conn,
    )


def generate_new_customers(
    count: int,
    cities: List[Dict],
    rng: np.random.Generator,
    fake: Faker,
    run_date: date,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate new customer registrations and their primary address.

    Returns (customers_df, addresses_df).
    """
    now = datetime.now(timezone.utc)
    customer_rows = []
    address_rows = []

    for _ in range(count):
        customer_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))
        city_info = cities[int(rng.integers(0, len(cities)))]
        city = city_info["city"]
        state = city_info["state"]

        account_type = str(rng.choice(
            config.ACCOUNT_TYPE_VALUES,
            p=config.ACCOUNT_TYPE_PROBS,
        ))

        if account_type == "D2C":
            segment = str(rng.choice(
                config.D2C_SEGMENT_VALUES,
                p=config.D2C_SEGMENT_PROBS,
            ))
        else:
            segment = config.B2B_SEGMENT

        full_name = fake.name()
        name_parts = full_name.lower().replace(".", "").split()
        first = name_parts[0] if name_parts else "customer"
        last = name_parts[-1] if len(name_parts) > 1 else "velora"
        email = f"{first}.{last}.{abs(hash(customer_id)) % 9999}@{'gmail.com' if rng.random() > 0.3 else 'yahoo.com'}"

        has_phone = rng.random() > 0.10
        phone = fake.numerify("##########") if has_phone else None

        customer_rows.append({
            "customer_id":   customer_id,
            "full_name":     full_name,
            "email":         email,
            "phone":         phone,
            "segment":       segment,
            "city":          city,
            "state":         state,
            "account_type":  account_type,
            "is_active":     1,
            "created_at":    now,
            "updated_at":    now,
            "source_system": "VELORA_CRM",
        })

        address_id = str(uuid.UUID(int=int(rng.integers(0, 2**31)) + (int(rng.integers(0, 2**31)) << 32)))
        pincodes = _PINCODES_BY_CITY.get(city, ["000000"])
        pincode = pincodes[int(rng.integers(0, len(pincodes)))]
        address_type = "HOME" if account_type == "D2C" else "BILLING"

        address_rows.append({
            "address_id":    address_id,
            "customer_id":   customer_id,
            "address_line":  fake.street_address(),
            "city":          city,
            "state":         state,
            "pincode":       pincode,
            "is_primary":    1,
            "address_type":  address_type,
            "created_at":    now,
            "updated_at":    now,
            "source_system": "VELORA_CRM",
        })

    return pd.DataFrame(customer_rows), pd.DataFrame(address_rows)


def generate_customer_updates(
    existing_df: pd.DataFrame,
    count: int,
    rng: np.random.Generator,
    cities: List[Dict],
    run_date: date,
) -> pd.DataFrame:
    """
    Generate SCD Type 2 update events: segment upgrades and city relocations.

    Returns a DataFrame of (customer_id, field, new_value, updated_at) that
    main.py translates into SQL UPDATEs. The downstream Silver notebook detects
    changes and opens new SCD rows in dim_customer.
    """
    if existing_df.empty or count == 0:
        return pd.DataFrame()

    now = datetime.now(timezone.utc)
    actual_count = min(count, len(existing_df))

    indices = rng.choice(len(existing_df), size=actual_count, replace=False)
    sampled = existing_df.iloc[indices].copy()

    updates = []
    for _, row in sampled.iterrows():
        update_type = rng.choice(["segment_upgrade", "city_relocation"], p=[0.60, 0.40])

        new_segment = row["segment"]
        new_city = row["city"]
        new_state = row["state"]

        if update_type == "segment_upgrade" and row["segment"] == "INDIVIDUAL":
            new_segment = "VIP"
        elif update_type == "city_relocation":
            other_cities = [c for c in cities if c["city"] != row["city"]]
            new_city_info = other_cities[int(rng.integers(0, len(other_cities)))]
            new_city = new_city_info["city"]
            new_state = new_city_info["state"]

        updates.append({
            "customer_id": row["customer_id"],
            "segment":     new_segment,
            "city":        new_city,
            "state":       new_state,
            "updated_at":  now,
        })

    return pd.DataFrame(updates)


def write_new_customers(customers_df: pd.DataFrame,
                        addresses_df: pd.DataFrame,
                        cursor) -> None:
    """INSERT new customers and their addresses to Azure SQL."""
    _insert_customers(customers_df, cursor)
    _insert_addresses(addresses_df, cursor)
    logger.info(
        "Inserted %d new customers, %d addresses",
        len(customers_df), len(addresses_df),
    )


def write_customer_updates(updates_df: pd.DataFrame, cursor) -> None:
    """UPDATE existing customer records for SCD change events."""
    if updates_df.empty:
        return

    sql = """
    UPDATE velora_crm.customers
    SET segment = ?, city = ?, state = ?, updated_at = ?
    WHERE customer_id = ?
    """
    rows = [
        (r["segment"], r["city"], r["state"], r["updated_at"], r["customer_id"])
        for _, r in updates_df.iterrows()
    ]
    cursor.executemany(sql, rows)
    logger.info("Updated %d customer records (SCD events)", len(rows))


def _insert_customers(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["customer_id", "full_name", "email", "phone", "segment",
            "city", "state", "account_type", "is_active",
            "created_at", "updated_at", "source_system"]
    sql = f"INSERT INTO velora_crm.customers ({', '.join(cols)}) VALUES ({', '.join(['?']*len(cols))})"
    rows = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])


def _insert_addresses(df: pd.DataFrame, cursor) -> None:
    if df.empty:
        return
    cols = ["address_id", "customer_id", "address_line", "city", "state",
            "pincode", "is_primary", "address_type",
            "created_at", "updated_at", "source_system"]
    sql = f"INSERT INTO velora_crm.customer_addresses ({', '.join(cols)}) VALUES ({', '.join(['?']*len(cols))})"
    rows = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    batch = config.SQL_INSERT_BATCH_SIZE
    for i in range(0, len(rows), batch):
        cursor.executemany(sql, rows[i: i + batch])
