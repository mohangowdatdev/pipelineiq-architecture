import os
from datetime import date
from typing import Dict, List

RANDOM_SEED = 42

# ── Day-of-week volume multipliers ────────────────────────────────────────────
# 0 = Monday, 6 = Sunday. Friday peak reflects payday + weekend-start shopping.
DOW_MULTIPLIERS: Dict[int, float] = {
    0: 1.00,  # Monday
    1: 1.00,  # Tuesday
    2: 1.05,  # Wednesday
    3: 1.10,  # Thursday
    4: 1.35,  # Friday — payday and weekend-start
    5: 1.25,  # Saturday
    6: 0.70,  # Sunday
}

# ── Monthly seasonal multipliers (Indian retail calendar) ─────────────────────
# November/December = 2x for Diwali + Black Friday + year-end sales cycle.
SEASONAL_MULTIPLIERS: Dict[int, float] = {
    1: 0.85,   # January  — post-festival demand drop
    2: 0.90,   # February
    3: 1.00,   # March    — fiscal year-end clearance
    4: 0.95,   # April    — post-Holi lull
    5: 0.95,   # May
    6: 0.90,   # June     — summer slowdown
    7: 1.00,   # July
    8: 1.05,   # August   — Independence Day promotions
    9: 1.15,   # September — pre-Navratri
    10: 1.20,  # October  — Navratri / Dussehra
    11: 2.00,  # November — Diwali + Black Friday peak
    12: 2.00,  # December — Christmas / year-end clearance
}

# ── Daily base volume ranges ──────────────────────────────────────────────────
# These are BEFORE applying day-of-week and seasonal multipliers.
VOLUME_D2C_MIN = 150
VOLUME_D2C_MAX = 300
VOLUME_B2B_MIN = 20
VOLUME_B2B_MAX = 50
VOLUME_STORE_MIN = 80
VOLUME_STORE_MAX = 150
VOLUME_STATUS_UPDATES_MIN = 200
VOLUME_STATUS_UPDATES_MAX = 400
VOLUME_NEW_CUSTOMERS_MIN = 15
VOLUME_NEW_CUSTOMERS_MAX = 40
VOLUME_CUSTOMER_UPDATES_MIN = 5
VOLUME_CUSTOMER_UPDATES_MAX = 15

# ── Territory and city master ─────────────────────────────────────────────────
# 8 Indian cities covering Velora's retail footprint.
CITIES: List[Dict] = [
    {"city": "Bangalore",  "state": "Karnataka",    "territory_id": "TER-001", "territory_name": "South 1", "region": "South"},
    {"city": "Chennai",    "state": "Tamil Nadu",   "territory_id": "TER-002", "territory_name": "South 2", "region": "South"},
    {"city": "Hyderabad",  "state": "Telangana",    "territory_id": "TER-003", "territory_name": "South 3", "region": "South"},
    {"city": "Mumbai",     "state": "Maharashtra",  "territory_id": "TER-004", "territory_name": "West 1",  "region": "West"},
    {"city": "Pune",       "state": "Maharashtra",  "territory_id": "TER-005", "territory_name": "West 2",  "region": "West"},
    {"city": "Delhi",      "state": "Delhi",        "territory_id": "TER-006", "territory_name": "North 1", "region": "North"},
    {"city": "Kolkata",    "state": "West Bengal",  "territory_id": "TER-007", "territory_name": "East 1",  "region": "East"},
    {"city": "Ahmedabad",  "state": "Gujarat",      "territory_id": "TER-008", "territory_name": "West 3",  "region": "West"},
]

CITY_NAMES = [c["city"] for c in CITIES]
CITY_TERRITORY_MAP = {c["city"]: c["territory_id"] for c in CITIES}
CITY_STATE_MAP = {c["city"]: c["state"] for c in CITIES}

# ── Divisions and SKU counts ──────────────────────────────────────────────────
DIVISIONS = [
    "CONSUMER_ELECTRONICS",
    "HOME_APPLIANCES",
    "PERSONAL_CARE",
    "SPORTS_FITNESS",
    "PREMIUM_ACCESSORIES",
]

DIVISION_SKU_COUNTS: Dict[str, int] = {
    "CONSUMER_ELECTRONICS": 1200,
    "HOME_APPLIANCES":      900,
    "PERSONAL_CARE":        700,
    "SPORTS_FITNESS":       700,
    "PREMIUM_ACCESSORIES":  700,
}

# Price ranges per division in INR (list_price)
DIVISION_PRICE_RANGES: Dict[str, tuple] = {
    "CONSUMER_ELECTRONICS": (2_000,   150_000),
    "HOME_APPLIANCES":      (3_000,    80_000),
    "PERSONAL_CARE":        (  100,     3_000),
    "SPORTS_FITNESS":       (  200,    40_000),
    "PREMIUM_ACCESSORIES":  (1_000,    50_000),
}

# Cost price is typically 55-70% of list price
COST_PRICE_RATIO_MIN = 0.55
COST_PRICE_RATIO_MAX = 0.70

# ── Brands per division ───────────────────────────────────────────────────────
DIVISION_BRANDS: Dict[str, List[str]] = {
    "CONSUMER_ELECTRONICS": ["Samsung", "Apple", "Sony", "LG", "OnePlus", "MI", "Lenovo", "ASUS", "Philips", "Bosch"],
    "HOME_APPLIANCES":      ["LG", "Samsung", "Whirlpool", "Haier", "Voltas", "Blue Star", "IFB", "Bajaj", "Godrej", "Panasonic"],
    "PERSONAL_CARE":        ["Himalaya", "Mamaearth", "Lakme", "L'Oreal", "Dove", "Pantene", "Vaseline", "Nivea", "Dabur", "Biotique"],
    "SPORTS_FITNESS":       ["Nike", "Adidas", "Puma", "Reebok", "Decathlon", "Cultsport", "Healthkart", "Nivia", "Vector X", "Cosco"],
    "PREMIUM_ACCESSORIES":  ["Titan", "Fossil", "Hidesign", "Baggit", "Fastrack", "Caprese", "Tommy Hilfiger", "Coach", "Biba", "W"],
}

# ── Product categories ────────────────────────────────────────────────────────
PRODUCT_CATEGORIES: List[Dict] = [
    # Consumer Electronics
    {"category_id": "CAT-CE-001", "category_name": "Smartphones",        "sub_category": "Mobile Phones",  "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-002", "category_name": "Laptops",            "sub_category": "Computers",      "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-003", "category_name": "Tablets",            "sub_category": "Computers",      "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-004", "category_name": "Headphones",         "sub_category": "Audio",          "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-005", "category_name": "Smart TVs",          "sub_category": "Television",     "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-006", "category_name": "Cameras",            "sub_category": "Photography",    "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-007", "category_name": "Smartwatches",       "sub_category": "Wearables",      "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-008", "category_name": "Speakers",           "sub_category": "Audio",          "division": "CONSUMER_ELECTRONICS"},
    {"category_id": "CAT-CE-009", "category_name": "Gaming Consoles",    "sub_category": "Gaming",         "division": "CONSUMER_ELECTRONICS"},
    # Home Appliances
    {"category_id": "CAT-HA-001", "category_name": "Refrigerators",      "sub_category": "Kitchen",        "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-002", "category_name": "Washing Machines",   "sub_category": "Laundry",        "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-003", "category_name": "Air Conditioners",   "sub_category": "Climate",        "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-004", "category_name": "Microwave Ovens",    "sub_category": "Kitchen",        "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-005", "category_name": "Dishwashers",        "sub_category": "Kitchen",        "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-006", "category_name": "Vacuum Cleaners",    "sub_category": "Cleaning",       "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-007", "category_name": "Water Purifiers",    "sub_category": "Water",          "division": "HOME_APPLIANCES"},
    {"category_id": "CAT-HA-008", "category_name": "Induction Cooktops", "sub_category": "Kitchen",        "division": "HOME_APPLIANCES"},
    # Personal Care
    {"category_id": "CAT-PC-001", "category_name": "Face Care",          "sub_category": "Skincare",       "division": "PERSONAL_CARE"},
    {"category_id": "CAT-PC-002", "category_name": "Hair Care",          "sub_category": "Hair",           "division": "PERSONAL_CARE"},
    {"category_id": "CAT-PC-003", "category_name": "Body Care",          "sub_category": "Body",           "division": "PERSONAL_CARE"},
    {"category_id": "CAT-PC-004", "category_name": "Oral Care",          "sub_category": "Dental",         "division": "PERSONAL_CARE"},
    {"category_id": "CAT-PC-005", "category_name": "Skincare",           "sub_category": "Skincare",       "division": "PERSONAL_CARE"},
    {"category_id": "CAT-PC-006", "category_name": "Fragrances",         "sub_category": "Fragrance",      "division": "PERSONAL_CARE"},
    # Sports & Fitness
    {"category_id": "CAT-SF-001", "category_name": "Athletic Footwear",  "sub_category": "Footwear",       "division": "SPORTS_FITNESS"},
    {"category_id": "CAT-SF-002", "category_name": "Yoga & Pilates",     "sub_category": "Equipment",      "division": "SPORTS_FITNESS"},
    {"category_id": "CAT-SF-003", "category_name": "Strength Training",  "sub_category": "Equipment",      "division": "SPORTS_FITNESS"},
    {"category_id": "CAT-SF-004", "category_name": "Cardio Equipment",   "sub_category": "Equipment",      "division": "SPORTS_FITNESS"},
    {"category_id": "CAT-SF-005", "category_name": "Sports Nutrition",   "sub_category": "Nutrition",      "division": "SPORTS_FITNESS"},
    {"category_id": "CAT-SF-006", "category_name": "Sports Accessories", "sub_category": "Accessories",    "division": "SPORTS_FITNESS"},
    # Premium Accessories
    {"category_id": "CAT-PA-001", "category_name": "Bags & Handbags",    "sub_category": "Bags",           "division": "PREMIUM_ACCESSORIES"},
    {"category_id": "CAT-PA-002", "category_name": "Watches",            "sub_category": "Timekeeping",    "division": "PREMIUM_ACCESSORIES"},
    {"category_id": "CAT-PA-003", "category_name": "Eyewear",            "sub_category": "Eyewear",        "division": "PREMIUM_ACCESSORIES"},
    {"category_id": "CAT-PA-004", "category_name": "Belts & Wallets",    "sub_category": "Leather Goods",  "division": "PREMIUM_ACCESSORIES"},
    {"category_id": "CAT-PA-005", "category_name": "Scarves & Wraps",    "sub_category": "Fashion",        "division": "PREMIUM_ACCESSORIES"},
    {"category_id": "CAT-PA-006", "category_name": "Jewellery",          "sub_category": "Jewellery",      "division": "PREMIUM_ACCESSORIES"},
]

CATEGORY_IDS_BY_DIVISION: Dict[str, List[str]] = {}
for cat in PRODUCT_CATEGORIES:
    CATEGORY_IDS_BY_DIVISION.setdefault(cat["division"], []).append(cat["category_id"])

# ── Store layout: 45 stores across 8 cities ───────────────────────────────────
# Distribution: Bangalore 8, Mumbai 8, Delhi 7, Hyderabad 6, Chennai 5,
#               Pune 4, Kolkata 4, Ahmedabad 3 = 45 total
# Tier assignment:
#   FLAGSHIP  — first store in each top-5 city (5 total)
#   STANDARD  — next tranche in large cities (25 total)
#   EXPRESS   — remaining smaller-format stores (15 total)

_STORE_CITY_COUNTS = [
    ("Bangalore",  8),
    ("Mumbai",     8),
    ("Delhi",      7),
    ("Hyderabad",  6),
    ("Chennai",    5),
    ("Pune",       4),
    ("Kolkata",    4),
    ("Ahmedabad",  3),
]

_FLAGSHIP_CITIES = {"Bangalore", "Mumbai", "Delhi", "Hyderabad", "Chennai"}

STORES: List[Dict] = []
_store_seq = 1
for _city_name, _count in _STORE_CITY_COUNTS:
    _city_info = next(c for c in CITIES if c["city"] == _city_name)
    for _i in range(_count):
        if _i == 0 and _city_name in _FLAGSHIP_CITIES:
            _tier = "FLAGSHIP"
        elif _store_seq <= 30:
            _tier = "STANDARD"
        else:
            _tier = "EXPRESS"
        STORES.append({
            "store_id":     f"STR-{_store_seq:03d}",
            "store_name":   f"Velora {_city_name} {'Flagship' if _tier == 'FLAGSHIP' else str(_i + 1)}",
            "city":         _city_info["city"],
            "state":        _city_info["state"],
            "territory_id": _city_info["territory_id"],
            "store_tier":   _tier,
            "is_active":    True,
        })
        _store_seq += 1

STORE_IDS = [s["store_id"] for s in STORES]

# ── Sales rep territory distribution: 30 reps across 8 territories ────────────
REP_TERRITORY_DISTRIBUTION: Dict[str, int] = {
    "TER-001": 5,  # Bangalore — largest territory
    "TER-002": 4,  # Chennai
    "TER-003": 4,  # Hyderabad
    "TER-004": 5,  # Mumbai — largest territory
    "TER-005": 3,  # Pune
    "TER-006": 5,  # Delhi
    "TER-007": 2,  # Kolkata
    "TER-008": 2,  # Ahmedabad
}  # Total: 30

# ── Account type and segment distributions ────────────────────────────────────
ACCOUNT_TYPE_PROBS = [0.80, 0.20]   # D2C, B2B
ACCOUNT_TYPE_VALUES = ["D2C", "B2B"]

D2C_SEGMENT_PROBS = [0.70, 0.20, 0.10]   # INDIVIDUAL, BUSINESS (crossover), VIP
D2C_SEGMENT_VALUES = ["INDIVIDUAL", "BUSINESS", "VIP"]

B2B_SEGMENT = "BUSINESS"

# ── Order line counts per channel ─────────────────────────────────────────────
D2C_LINES_MIN, D2C_LINES_MAX = 1, 5
B2B_LINES_MIN, B2B_LINES_MAX = 3, 15
STORE_LINES_MIN, STORE_LINES_MAX = 1, 4

# ── B2B pricing ───────────────────────────────────────────────────────────────
B2B_DISCOUNT_MIN_PCT = 5
B2B_DISCOUNT_MAX_PCT = 15

# ── Status progression rules ──────────────────────────────────────────────────
# Each status transitions to target after min_age_days since order creation.
STATUS_PROGRESSION = {
    "PENDING":    {"target": "PROCESSING", "min_age_days": 1},
    "PROCESSING": {"target": "SHIPPED",    "min_age_days": 3},
    "SHIPPED":    {"target": "DELIVERED",  "min_age_days": 7},
}
RETURN_INITIATION_RATE = 0.02  # 2% of DELIVERED orders

# ── Database connection ───────────────────────────────────────────────────────
SQL_SERVER   = os.getenv("AZURE_SQL_SERVER",   "")
SQL_DATABASE = os.getenv("AZURE_SQL_DATABASE", "velora")
SQL_USERNAME = os.getenv("AZURE_SQL_USERNAME", "")
SQL_PASSWORD = os.getenv("AZURE_SQL_PASSWORD", "")
SQL_DRIVER   = "ODBC Driver 18 for SQL Server"

# Auth mode: "password" (default, for laptop/CI) or "msi" (Azure Function with
# system-assigned managed identity). When "msi", UID/PWD are ignored and ODBC
# negotiates a token via the host's MSI endpoint.
SQL_AUTH_MODE = os.getenv("AZURE_SQL_AUTH_MODE", "password").lower()

SQL_INSERT_BATCH_SIZE = 1_000  # rows per executemany batch


def get_connection_string() -> str:
    base = (
        f"DRIVER={{{SQL_DRIVER}}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=90;"
    )
    if SQL_AUTH_MODE == "msi":
        # System-assigned managed identity (Azure Function MSI auth).
        return base + "Authentication=ActiveDirectoryMsi;"
    # Password auth — laptop / CI.
    return base + f"UID={SQL_USERNAME};PWD={SQL_PASSWORD};"


def get_adjusted_volume(base_min: int, base_max: int, run_date: date,
                        rng) -> int:
    """Return volume scaled by day-of-week and seasonal multipliers."""
    dow_mult = DOW_MULTIPLIERS[run_date.weekday()]
    season_mult = SEASONAL_MULTIPLIERS[run_date.month]
    base = int(rng.integers(base_min, base_max + 1))
    return max(1, int(base * dow_mult * season_mult))
