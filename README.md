# CS2 / Steam Inventory — Home Assistant Integration

Track the market value of your Steam inventory directly in Home Assistant.
Multiple accounts, automatic detection of every game with marketable items
(CS2, TF2, Dota 2, Rust, PUBG, trading cards…), configurable currency, P&L
tracking, optional CS2 float values, a watchlist for items you don't own yet,
long-term price history in the recorder, and ready-to-use Lovelace dashboards.

---

## Features

- **Multi-account** — monitor several Steam profiles at once; identical items
  across accounts are merged (quantity is summed).
- **Auto-discovery** — 26 marketable games are probed automatically; only the
  ones you actually own are tracked.
- **Configurable currency** — prices are fetched and reported in the currency
  you choose (EUR by default).
- **Live prices** — Steam Market prices fetched on a rolling basis with
  rate-limit / IP-ban protection and stale-data fallback.
- **P&L tracking** — record buy prices and get ROI, profit (gross/net of the
  15% Steam tax), and best/worst performers.
- **CS2 float values** — optional, via the public CSGOFloat API.
- **Watchlist** — track prices of items you don't own, with target alerts.
- **Long-term statistics** — backfill years of Steam Market price history into
  the HA recorder as external statistics (`cs2:*`).
- **Lovelace dashboards** — generate complete dashboard YAML files.

---

## Requirements

- Home Assistant **2024.1+** (the `recorder` integration must be enabled — it is
  by default).
- A **public** Steam inventory (Steam → Profile → Privacy → *Inventory: Public*).
- One or more **Steam64 IDs** (17 digits). Find yours at
  [steamid.io](https://steamid.io).

---

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/daoudlab/hacs-cs2` · Category **Integration**.
3. Install **CS2 Inventory**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration** → search **CS2 Inventory**.

---

## Configuration

### Step 1 — Steam accounts

One or more IDs as `steamid:label`, comma-separated:

```
76561190000000001:main,76561190000000002:alt
```

### Step 2 — Settings

All of these are editable later via the integration's **Configure** (options) screen.

| Option | Default | Range | Description |
|---|---|---|---|
| Scan interval (min) | `5` | 5–1440 | How often a cycle runs. Each cycle prices a rolling chunk of items. |
| Strict missing ratio | `0.3` | 0.0–1.0 | If more than this fraction of a game's prices are missing, keep the previous (stale) values for that cycle. |
| Minimum item value | `0` | ≥ 0 | Ignore items worth less than this (0 = keep all). |
| Maximum items per game | `0` | ≥ 0 | Cap the number of priced items per game (0 = no cap). The most valuable items are kept. |
| History days | `730` | 30–3650 | Default window used by `run_import` when no start date is given. |
| Currency | `EUR` | — | Steam Market currency; also the unit of all monetary sensors. |
| Include trading cards | `Off` | — | Also track Steam Community trading cards (App 753). |
| Fetch CS2 float values | `Off` | — | Fetch float values via CSGOFloat (CS2 only). |

> **Currency note:** 2-decimal currencies (EUR, USD, GBP, CAD…) are fully
> supported. 0-decimal currencies (JPY, KRW) are not yet handled correctly.

### Step 3 — Historical import (optional)

Paste your `steamLoginSecure` cookie to backfill price history into the
recorder. The cookie is used only for that request and is **never stored**.
Leave it blank to skip.

---

## Services

All services require an admin user.

| Service | Purpose |
|---|---|
| `cs2.force_refresh` | Run an inventory + price cycle immediately. |
| `cs2.run_import` | Backfill Steam Market price history into recorder statistics. |
| `cs2.generate_dashboards` | Write Lovelace dashboard YAML files (needs data — run after the first scan). |
| `cs2.set_buy_price` | Set/remove an item's purchase price (for ROI/P&L). |
| `cs2.watchlist_add` | Add/update a watched item (with optional target price). |
| `cs2.watchlist_remove` | Remove a watched item. |

```yaml
# Record a purchase price (price in your configured currency; 0 removes it)
action: cs2.set_buy_price
data:
  market_hash_name: "AK-47 | Redline (Field-Tested)"
  price: 12.50

# Backfill price history (start date / min value are optional)
action: cs2.run_import
data:
  steam_cookie: "<your steamLoginSecure value>"
  import_start_date: "2015-01-01"
  min_item_value: 0

# Watch an item not in your inventory
action: cs2.watchlist_add
data:
  market_hash_name: "AWP | Dragon Lore (Factory New)"
  target_price: 45.00
  appid: 730
  note: "buy below target"
```

---

## Sensors

A single **Steam Inventory** device is created. Entity IDs follow HA's
`has_entity_name` scheme (`steam_inventory_` prefix):

| Entity | Description |
|---|---|
| `sensor.steam_inventory_total` | Global portfolio value (all games), with P&L attributes. |
| `sensor.steam_inventory_<game>_total` | Per-game total (e.g. `…_cs2_total`). |
| `sensor.steam_inventory_<item_slug>` | Per-item current unit price; `quantity` is an attribute. |
| `sensor.steam_watch_<item_slug>` | Watchlist item price (+ `target_price`, `below_target`). |
| `sensor.steam_inventory_sync_status` | Health: `ok` / `degraded` / `rate_limited` / `market_limited` / `error` / `no_games` (+ `items_count`, `items_total_qty`, `missing_count`, `banned_accounts`…). |

Item slugs are derived from the market hash name (e.g.
`AK-47 | Redline (Field-Tested)` → `steam_inventory_ak_47_redline_field_tested`).

---

## Dashboards

`cs2.generate_dashboards` writes, into `<config>/steam_dashboards/`:

- `steam_global.yaml` — portfolio / performance / administration views,
- `steam_<game>.yaml` — one detailed view per active game,
- `steam_watchlist.yaml` — when a watchlist exists.

These are **full YAML-mode dashboards**. Wire one in `configuration.yaml` (the
`url_path` must contain a dash) and restart HA:

```yaml
lovelace:
  dashboards:
    steam-inventory:
      mode: yaml
      filename: steam_dashboards/steam_global.yaml
      title: Steam
      icon: mdi:steam
      show_in_sidebar: true
```

The richer per-game views use HACS **frontend** cards. An integration cannot
install frontend plugins, so install these from HACS yourself (the integration
raises a notification listing them): **Mushroom**, **ApexCharts Card**,
**Vertical Stack In Card**, **Layout Card**, **Expander Card**, **card-mod**.

---

## Buy prices & watchlist files

The services above edit these files, but you can also create them by hand in
`<config>/`:

```jsonc
// cs2_buy_prices.json  — purchase prices for ROI/P&L
{ "AK-47 | Redline (Field-Tested)": 12.50, "AWP | Asiimov (Battle-Scarred)": 35.00 }
```

```jsonc
// cs2_watchlist.json — items you don't own
[ { "market_hash_name": "AK-47 | Asiimov (Field-Tested)", "appid": 730, "target_price": 45.00, "note": "" } ]
```

---

## Privacy

- Steam **session cookies are never stored** in the config entry or written to
  logs — they are used only for the on-demand history import.
- Market prices come from the public Steam Market endpoints; float values from
  the public CSGOFloat API (no authentication).
