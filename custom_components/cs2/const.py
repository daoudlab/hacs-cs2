"""Constants for the CS2 Inventory integration."""
DOMAIN = "cs2"

CONF_STEAM_IDS = "steam_ids"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STRICT_MISSING_RATIO = "strict_missing_ratio"
CONF_MIN_ITEM_VALUE = "min_item_value"
CONF_MAX_ITEMS = "max_items"

DEFAULT_SCAN_INTERVAL = 60       # minutes
DEFAULT_STRICT_RATIO = 0.30
DEFAULT_MIN_VALUE = 0.0
DEFAULT_MAX_ITEMS = 0            # 0 = no cap

SENSOR_TOTAL_ID = "sensor.cs2_inventory_total"
SENSOR_ITEM_PREFIX = "sensor.cs2_item_"
SENSOR_ACCOUNT_PREFIX = "sensor.cs2_inventory_total_"

STORAGE_VERSION = 1
STORAGE_KEY = "cs2_inventory_state"

# ── Steam API URLs ─────────────────────────────────────────────────────────────
STEAM_INVENTORY_URL = (
    "https://steamcommunity.com/inventory/{steam_id}/730/2?l=english&count=500"
)
STEAM_MARKET_PRICE_URL = (
    "https://steamcommunity.com/market/priceoverview/"
    "?appid=730&currency=3&market_hash_name={name}"
)
STEAM_PROFILE_XML_URL = "https://steamcommunity.com/profiles/{steam_id}/?xml=1"

# ── CSGOFloat ──────────────────────────────────────────────────────────────────
CSGOFLOAT_API_URL = "https://api.csgofloat.com/?url={inspect_url}"
CSGOFLOAT_HEALTHCHECK = "https://api.csgofloat.com/healthcheck"
CSGOFLOAT_TIMEOUT = 10

# ── Steam Market rate limiting ─────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 3.5
REQUESTS_BEFORE_PAUSE = 20
PAUSE_SECONDS = 15
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2
MAX_BACKOFF = 180

# ── Inventory ─────────────────────────────────────────────────────────────────
INVENTORY_PAGE_DELAY = 2
STEAM_TAX = 0.85

# ── HTTP headers ──────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}
