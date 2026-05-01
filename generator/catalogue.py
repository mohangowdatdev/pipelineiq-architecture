"""
Static master data for Velora Retail Group.

Generated deterministically from RANDOM_SEED. All IDs use UUID5 so the same
seed always produces the same UUID for the same logical entity. Run seed_to_db()
once after the Azure SQL schema is bootstrapped. Subsequent runs call
load_from_db() to pull the catalogue into memory.
"""

import uuid
import logging
from datetime import date, datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd
from faker import Faker

import config

logger = logging.getLogger(__name__)

# UUID namespace for all Velora catalogue entities
_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _uid(namespace: str, value: str) -> str:
    """Return a deterministic UUID5 string for a catalogue entity."""
    return str(uuid.uuid5(_NS, f"{namespace}:{value}"))


# ── Product name templates per division ──────────────────────────────────────
_PRODUCT_TEMPLATES: Dict[str, List[str]] = {
    "CONSUMER_ELECTRONICS": [
        "{brand} {series} Pro {num}",
        "{brand} Galaxy {series} {num}",
        "{brand} {series} Elite {num}",
        "{brand} Ultra {series} {num}",
        "{brand} {series} Max {num}",
    ],
    "HOME_APPLIANCES": [
        "{brand} {capacity}L {series}",
        "{brand} {series} Plus {num}",
        "{brand} {capacity}W {series}",
        "{brand} {series} Smart {num}",
        "{brand} {series} Pro {num}",
    ],
    "PERSONAL_CARE": [
        "{brand} {series} {num}ml",
        "{brand} {series} Formula",
        "{brand} {series} Advanced",
        "{brand} {series} Natural {num}",
        "{brand} {series} Care",
    ],
    "SPORTS_FITNESS": [
        "{brand} {series} Pro {num}",
        "{brand} Air {series} {num}",
        "{brand} {series} Training",
        "{brand} {series} Elite {num}",
        "{brand} {series} X {num}",
    ],
    "PREMIUM_ACCESSORIES": [
        "{brand} {series} Collection",
        "{brand} {series} Classic {num}",
        "{brand} {series} Edition",
        "{brand} {series} Premium {num}",
        "{brand} {series} Signature",
    ],
}

_SERIES_NAMES = [
    "Alpha", "Beta", "Delta", "Omega", "Nova", "Apex", "Edge", "Core",
    "Pulse", "Zen", "Sync", "Arc", "Wave", "Flow", "Peak", "Matrix",
    "Fusion", "Nexus", "Vibe", "Echo", "Spark", "Swift", "Bold", "Rise",
]

_CAPACITIES = [150, 200, 250, 300, 350, 400, 500, 600, 750, 1000, 1200, 1500]


def _make_product_name(division: str, brand: str, seq: int,
                       rng: np.random.Generator) -> str:
    templates = _PRODUCT_TEMPLATES[division]
    template = templates[seq % len(templates)]
    series = _SERIES_NAMES[seq % len(_SERIES_NAMES)]
    cap = _CAPACITIES[seq % len(_CAPACITIES)]
    return template.format(
        brand=brand,
        series=series,
        num=seq % 100,
        capacity=cap,
    )


def _make_sku(division: str, category_id: str, seq: int) -> str:
    div_code = {
        "CONSUMER_ELECTRONICS": "CE",
        "HOME_APPLIANCES":      "HA",
        "PERSONAL_CARE":        "PC",
        "SPORTS_FITNESS":       "SF",
        "PREMIUM_ACCESSORIES":  "PA",
    }[division]
    cat_code = category_id.split("-")[-1]
    return f"VLR-{div_code}-{cat_code}-{seq:04d}"


def build_catalogue(seed: int = config.RANDOM_SEED) -> Dict:
    """
    Generate all static Velora master data deterministically.

    Returns a dict with DataFrames for: categories, products, product_pricing,
    stores, territories, sales_reps, territory_assignments.
    """
    rng = np.random.default_rng(seed)
    Faker.seed(seed)
    fake = Faker(["en_IN"])

    today = date.today()

    categories_df = _build_categories()
    products_df, pricing_df = _build_products(rng, today)
    stores_df = _build_stores()
    territories_df = _build_territories()
    reps_df, assignments_df = _build_sales_reps(fake, rng, today)

    logger.info(
        "Catalogue built: %d products, %d stores, %d reps",
        len(products_df), len(stores_df), len(reps_df),
    )

    return {
        "categories":            categories_df,
        "products":              products_df,
        "product_pricing":       pricing_df,
        "stores":                stores_df,
        "territories":           territories_df,
        "sales_reps":            reps_df,
        "territory_assignments": assignments_df,
    }


def _build_categories() -> pd.DataFrame:
    rows = []
    for cat in config.PRODUCT_CATEGORIES:
        rows.append({
            "category_id":   cat["category_id"],
            "category_name": cat["category_name"],
            "sub_category":  cat["sub_category"],
            "division":      cat["division"],
            "is_active":     True,
        })
    return pd.DataFrame(rows)


def _build_products(rng: np.random.Generator, today: date) -> tuple:
    now = datetime.now(timezone.utc)
    product_rows = []
    pricing_rows = []
    global_seq = 1

    for division, sku_count in config.DIVISION_SKU_COUNTS.items():
        cat_ids = config.CATEGORY_IDS_BY_DIVISION[division]
        brands = config.DIVISION_BRANDS[division]
        price_min, price_max = config.DIVISION_PRICE_RANGES[division]

        for i in range(sku_count):
            category_id = cat_ids[i % len(cat_ids)]
            brand = brands[i % len(brands)]
            sku = _make_sku(division, category_id, global_seq)
            product_id = _uid("product", sku)
            product_name = _make_product_name(division, brand, i, rng)

            # Launched between 2020-01-01 and today (earlier products launched earlier)
            days_ago = int(rng.integers(30, 1800))
            launched = date.fromordinal(max(
                today.toordinal() - days_ago,
                date(2020, 1, 1).toordinal()
            ))

            product_rows.append({
                "product_id":    product_id,
                "sku":           sku,
                "product_name":  product_name,
                "category_id":   category_id,
                "division":      division,
                "brand":         brand,
                "is_active":     True,
                "launched_date": launched,
                "created_at":    now,
                "updated_at":    now,
                "source_system": "VELORA_PIM",
            })

            # Current active pricing record
            list_price = round(float(rng.uniform(price_min, price_max)), 2)
            cost_ratio = float(rng.uniform(
                config.COST_PRICE_RATIO_MIN, config.COST_PRICE_RATIO_MAX
            ))
            cost_price = round(list_price * cost_ratio, 2)
            pricing_id = _uid("pricing", f"{sku}:initial")

            pricing_rows.append({
                "pricing_id":     pricing_id,
                "product_id":     product_id,
                "list_price":     list_price,
                "cost_price":     cost_price,
                "effective_from": date(2024, 1, 1),
                "effective_to":   None,
                "pricing_type":   "STANDARD",
                "created_at":     now,
                "source_system":  "VELORA_PIM",
            })

            global_seq += 1

    return pd.DataFrame(product_rows), pd.DataFrame(pricing_rows)


def _build_stores() -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    rows = []
    for store in config.STORES:
        rows.append({
            **store,
            "opened_date":   date(2018, 1, 1),
            "_pipeline_run_id": "SEED",
        })
    return pd.DataFrame(rows)


def _build_territories() -> pd.DataFrame:
    rows = []
    for city in config.CITIES:
        rows.append({
            "territory_id":   city["territory_id"],
            "territory_name": city["territory_name"],
            "city":           city["city"],
            "state":          city["state"],
            "region":         city["region"],
            "is_active":      True,
            "_pipeline_run_id": "SEED",
        })
    return pd.DataFrame(rows)


def _build_sales_reps(fake: Faker, rng: np.random.Generator,
                      today: date) -> tuple:
    now = datetime.now(timezone.utc)
    rep_rows = []
    assignment_rows = []
    rep_seq = 1

    for territory_id, count in config.REP_TERRITORY_DISTRIBUTION.items():
        for i in range(count):
            rep_id = _uid("rep", f"REP-{rep_seq:03d}")
            full_name = fake.name()
            first = full_name.split()[0].lower()
            last = full_name.split()[-1].lower()
            email = f"{first}.{last}.{rep_seq}@velora.co.in"
            days_ago = int(rng.integers(180, 2000))
            hire_date = date.fromordinal(
                max(today.toordinal() - days_ago, date(2018, 1, 1).toordinal())
            )

            rep_rows.append({
                "rep_id":        rep_id,
                "full_name":     full_name,
                "email":         email,
                "phone":         fake.numerify("##########") if rng.random() > 0.15 else None,
                "hire_date":     hire_date,
                "is_active":     True,
                "created_at":    now,
                "updated_at":    now,
                "source_system": "VELORA_HRM",
            })

            assignment_id = _uid("assignment", f"REP-{rep_seq:03d}:initial")
            assignment_rows.append({
                "assignment_id":  assignment_id,
                "rep_id":         rep_id,
                "territory_id":   territory_id,
                "assigned_from":  hire_date,
                "assigned_to":    None,
                "is_current":     True,
                "created_at":     now,
                "source_system":  "VELORA_HRM",
            })

            rep_seq += 1

    return pd.DataFrame(rep_rows), pd.DataFrame(assignment_rows)


# ── Database operations ───────────────────────────────────────────────────────

def is_catalogue_seeded(conn) -> bool:
    """Return True if products table already has rows."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(1) FROM velora_pim.products")
    count = cursor.fetchone()[0]
    return count > 0


def seed_to_db(catalogue: Dict, conn) -> None:
    """Write catalogue to Azure SQL. Call once after schema bootstrap."""
    cursor = conn.cursor()

    _bulk_insert(cursor, "velora_pim.product_categories",
                 ["category_id", "category_name", "sub_category", "division", "is_active"],
                 catalogue["categories"])

    _bulk_insert(cursor, "velora_pim.products",
                 ["product_id", "sku", "product_name", "category_id", "division",
                  "brand", "is_active", "launched_date",
                  "created_at", "updated_at", "source_system"],
                 catalogue["products"])

    _bulk_insert(cursor, "velora_pim.product_pricing",
                 ["pricing_id", "product_id", "list_price", "cost_price",
                  "effective_from", "effective_to", "pricing_type",
                  "created_at", "source_system"],
                 catalogue["product_pricing"])

    _bulk_insert(cursor, "velora_pim.stores",
                 ["store_id", "store_name", "city", "state",
                  "territory_id", "store_tier", "is_active", "opened_date"],
                 catalogue["stores"])

    _bulk_insert(cursor, "velora_hrm.sales_reps",
                 ["rep_id", "full_name", "email", "phone", "hire_date",
                  "is_active", "created_at", "updated_at", "source_system"],
                 catalogue["sales_reps"])

    _bulk_insert(cursor, "velora_hrm.territory_assignments",
                 ["assignment_id", "rep_id", "territory_id",
                  "assigned_from", "assigned_to", "is_current",
                  "created_at", "source_system"],
                 catalogue["territory_assignments"])

    logger.info("Catalogue seeded to Azure SQL")


def load_from_db(conn) -> Dict:
    """Load the catalogue from Azure SQL into memory."""
    products_df = pd.read_sql(
        "SELECT product_id, sku, product_name, category_id, division, brand, "
        "is_active, launched_date FROM velora_pim.products WHERE is_active = 1",
        conn
    )
    pricing_df = pd.read_sql(
        "SELECT product_id, list_price, cost_price FROM velora_pim.product_pricing "
        "WHERE effective_to IS NULL",
        conn
    )
    stores_df = pd.read_sql(
        "SELECT store_id, store_name, city, state, territory_id, store_tier, is_active "
        "FROM velora_pim.stores WHERE is_active = 1",
        conn
    )
    reps_df = pd.read_sql(
        "SELECT r.rep_id, r.full_name, ta.territory_id "
        "FROM velora_hrm.sales_reps r "
        "JOIN velora_hrm.territory_assignments ta ON r.rep_id = ta.rep_id "
        "WHERE r.is_active = 1 AND ta.is_current = 1",
        conn
    )

    return {
        "products": products_df,
        "pricing":  pricing_df,
        "stores":   stores_df,
        "reps":     reps_df,
    }


def _bulk_insert(cursor, table: str, columns: List[str],
                 df: pd.DataFrame) -> None:
    """Insert a DataFrame into an Azure SQL table in batches."""
    if df.empty:
        return

    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

    rows = [
        tuple(
            None if (v is None or (isinstance(v, float) and np.isnan(v)))
            else bool(v) if isinstance(v, (bool, np.bool_))
            else v
            for v in (row[col] for col in columns)
        )
        for _, row in df.iterrows()
    ]

    cursor.fast_executemany = True
    batch_size = config.SQL_INSERT_BATCH_SIZE
    for start in range(0, len(rows), batch_size):
        cursor.executemany(sql, rows[start: start + batch_size])

    logger.debug("Inserted %d rows into %s", len(rows), table)
