# Steam Inventory — Home Assistant Integration

Track your Steam inventory value in real time directly in Home Assistant. Supports multiple Steam accounts, all marketable games (CS2, TF2, Dota 2, etc.), price history graphs, P&L tracking, float values for CS2, and a watchlist for items you don't own yet.

---

## Features

- **Multi-account** — monitor several Steam profiles at once
- **Auto-discovery** — detects all games with marketable items automatically
- **Live prices** — fetches Steam Market prices with rate-limit protection
- **P&L tracking** — record buy prices and track ROI per item
- **CS2 float values** — optional via CSGOFloat API (opt-in)
- **Watchlist** — track prices of items you want to buy, with target alerts
- **Lovelace dashboards** — generate ready-to-use YAML dashboard files
- **Long-term statistics** — full price history injectable into HA recorder

---

## Requirements

- Home Assistant 2024.1+
- Public Steam inventory (Settings → Privacy → Inventory → Public)
- 64-bit Steam ID (17 digits) — find yours at [steamid.io](https://steamid.io)

---

## Installation via HACS

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add URL `https://github.com/your-username/hacs-cs2` · Category: **Integration**
3. Search for **Steam Inventory** and install
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** → search **Steam Inventory**

---

## Configuration

### Step 1 — Steam Accounts

Enter one or more Steam IDs in the format `steamid:name` separated by commas:

```
76561190000000001:main,76561190000000002:alt
```

### Step 2 — Settings

| Option | Default | Description |
|---|---|---|
| Scan interval | 60 min | How often to fetch prices (min: 5 min) |
| Strict missing ratio | 0.3 | Skip game if >30% of prices are unavailable |
| Minimum item value | 0.5 EUR | Ignore items cheaper than this threshold |
| Maximum items per game | 200 | Cap to avoid API rate limits on large inventories |
| Include trading cards | Off | Also track Steam trading cards (App 753) |
| Fetch CS2 float values | Off | Fetch float values via CSGOFloat API (CS2 only) |

### Step 3 — Historical Import (optional)

Provide your `steamLoginSecure` cookie to backfill price history into HA statistics. Leave blank to skip.

---

## Services

All services require admin access.

### `cs2.force_refresh`

Triggers an immediate inventory and price scan.

### `cs2.generate_dashboards`

Writes Lovelace YAML files to `<config>/steam_dashboards/`. Add them via **Settings → Dashboards → Add Dashboard → From YAML**.

### `cs2.set_buy_price`

Records the purchase price for an item. Used for ROI/P&L calculations.

```yaml
service: cs2.set_buy_price
data:
  market_hash_name: "AK-47 | Redline (Field-Tested)"
  price: 12.50
```

Set `price: 0` to remove the entry.

### `cs2.run_import`

Backfills Steam Market price history into HA recorder statistics.

```yaml
service: cs2.run_import
data:
  steam_cookie: "your_steamLoginSecure_value"
  import_start_date: "2024-01-01"   # optional
  min_item_value: 0.5               # optional
```

---

## Sensors

| Entity | Description |
|---|---|
| `sensor.steam_inventory_total` | Global portfolio value (all games) |
| `sensor.steam_{game}_total` | Per-game portfolio total |
| `sensor.steam_item_{game}_{slug}` | Per-item current price |
| `sensor.steam_watch_{slug}` | Watchlist item price |
| `sensor.steam_sync_status` | Integration health (ok / error / no_games) |

---

## Watchlist

Create `<config>/cs2_watchlist.json` to track items you don't own:

```json
[
  {
    "market_hash_name": "AK-47 | Asiimov (Field-Tested)",
    "appid": 730,
    "target_price": 45.00,
    "note": "Buy when below 45€"
  }
]
```

---

## Buy Prices

Create `<config>/cs2_buy_prices.json` or use the `cs2.set_buy_price` service:

```json
{
  "AK-47 | Redline (Field-Tested)": 12.50,
  "AWP | Asiimov (Battle-Scarred)": 35.00
}
```

---

## Privacy

- Steam session cookies are **never** stored in HA config or logs
- Prices are fetched from the public Steam Market API
- Float values use the public CSGOFloat API (no authentication required)
