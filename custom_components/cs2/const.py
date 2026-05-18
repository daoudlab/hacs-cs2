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
