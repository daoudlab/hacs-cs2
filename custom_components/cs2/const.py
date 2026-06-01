"""Constants for the CS2/Steam Inventory integration."""
DOMAIN = "cs2"

CONF_STEAM_IDS = "steam_ids"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STRICT_MISSING_RATIO = "strict_missing_ratio"
CONF_MIN_ITEM_VALUE = "min_item_value"
CONF_MAX_ITEMS = "max_items"
CONF_INCLUDE_TRADING_CARDS = "include_trading_cards"
CONF_HISTORY_DAYS = "history_days"

DEFAULT_SCAN_INTERVAL = 5        # minutes (rolling update: 5 items per cycle)
DEFAULT_FETCH_CHUNK_SIZE = 5     # items fetched per cycle (~1 req/min average)
DEFAULT_STRICT_RATIO = 0.30
DEFAULT_MIN_VALUE = 0.0
DEFAULT_MAX_ITEMS = 0            # 0 = no cap
DEFAULT_HISTORY_DAYS = 730       # 2 years of HA recorder statistics by default

# ── Entity IDs ─────────────────────────────────────────────────────────────────
# All IDs use "steam_inventory_" because has_entity_name=True + device "Steam Inventory"
# causes HA to prepend the device slug to the entity name slug.
SENSOR_TOTAL_ID = "sensor.steam_inventory_total"
SENSOR_GAME_PREFIX = "sensor.steam_inventory_"        # + slug + "_total"
SENSOR_ITEM_PREFIX = "sensor.steam_inventory_"        # + make_slug(market_hash_name)
SENSOR_SYNC_ID = "sensor.steam_inventory_sync_status"
# Watchlist sensors use a distinct prefix to avoid collision with inventory item sensors
# (a watchlist item and an owned item can have the same market_hash_name)
SENSOR_WATCHLIST_PREFIX = "sensor.steam_watch_"       # + make_slug(market_hash_name)

STORAGE_VERSION = 1
STORAGE_KEY = "cs2_inventory_state"

# ── Phase 2 — historical import ───────────────────────────────────────────────
CONF_IMPORT_START_DATE = "import_start_date"
CONF_STEAM_COOKIE = "steam_cookie"

STEAM_HISTORY_URL = (
    "https://steamcommunity.com/market/pricehistory/"
    "?appid={appid}&currency=3&market_hash_name={name}"
)

SERVICE_RUN_IMPORT = "run_import"
SERVICE_GENERATE_DASHBOARDS = "generate_dashboards"
SERVICE_FORCE_REFRESH = "force_refresh"
SERVICE_SET_BUY_PRICE = "set_buy_price"
SERVICE_WATCHLIST_ADD = "watchlist_add"
SERVICE_WATCHLIST_REMOVE = "watchlist_remove"

# ── Extra config keys ──────────────────────────────────────────────────────────
CONF_FETCH_FLOATS = "fetch_floats"

# ── Extra JSON files (in /config/) ────────────────────────────────────────────
WATCHLIST_FILE = "cs2_watchlist.json"
TARGETS_FILE = "cs2_price_targets.json"

# ── Steam API URLs ─────────────────────────────────────────────────────────────
STEAM_INVENTORY_URL = (
    "https://steamcommunity.com/inventory/{steam_id}/{appid}/{contextid}"
    "?l=english&count=500"
)
STEAM_MARKET_PRICE_URL = (
    "https://steamcommunity.com/market/priceoverview/"
    "?appid={appid}&currency=3&market_hash_name={name}"
)
STEAM_PROFILE_XML_URL = "https://steamcommunity.com/profiles/{steam_id}/?xml=1"

# ── Known marketable games (appid, contextid, slug, display_name) ─────────────
# contextid=2 for most games, contextid=6 for Steam Community (trading cards)
KNOWN_MARKETABLE_APPS: list[tuple[int, int, str, str]] = [
    (730, 2, "cs2", "CS2"),
    (570, 2, "dota2", "Dota 2"),
    (440, 2, "tf2", "TF2"),
    (252490, 2, "rust", "Rust"),
    (578080, 2, "pubg", "PUBG"),
    (433850, 2, "h1z1", "H1Z1"),
    (304930, 2, "unturned", "Unturned"),
    (322330, 2, "dst", "Don't Starve Together"),
    (232090, 2, "kf2", "Killing Floor 2"),
    (218620, 2, "payday2", "Payday 2"),
    (230410, 2, "warframe", "Warframe"),
    (513710, 2, "scum", "Scum"),
    (274940, 2, "depth", "Depth"),
    (321360, 2, "primal_carnage", "Primal Carnage: Extinction"),
    (583950, 2, "artifact", "Artifact"),
    (1269260, 2, "artifact_foundry", "Artifact Foundry"),
    (290300, 2, "armello", "Armello"),
    (489830, 2, "minion_masters", "Minion Masters"),
    (1203220, 2, "naraka", "Naraka: Bladepoint"),
    (394690, 2, "tower_unite", "Tower Unite"),
    (431240, 2, "golf_friends", "Golf With Your Friends"),
    (1782210, 2, "crab_game", "Crab Game"),
    (2923300, 2, "banana", "Banana"),
    (3033530, 2, "cats", "Cats"),
    (2784840, 2, "egg", "Egg"),
    # Opt-in: Steam Community cards (contextid=6)
    (753, 6, "steam_cards", "Steam Cards"),
]

# ── CSGOFloat ──────────────────────────────────────────────────────────────────
CSGOFLOAT_API_URL = "https://api.csgofloat.com/?url={inspect_url}"
CSGOFLOAT_HEALTHCHECK = "https://api.csgofloat.com/healthcheck"
CSGOFLOAT_TIMEOUT = 10

# ── Steam Market rate limiting ─────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 3.5
REQUESTS_BEFORE_PAUSE = 20
PAUSE_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MAX_BACKOFF = 60

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
