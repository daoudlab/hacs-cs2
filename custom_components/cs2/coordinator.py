"""DataUpdateCoordinator for CS2/Steam Inventory."""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import steam_inventory, steam_market
from .api.steam_inventory import InventoryBannedError
from .api.steam_market import RateLimits
from .compute import compute_item_metrics, compute_global_metrics
from .const import (
    DOMAIN,
    CONF_STEAM_IDS,
    CONF_SCAN_INTERVAL,
    CONF_STRICT_MISSING_RATIO,
    CONF_MIN_ITEM_VALUE,
    CONF_MAX_ITEMS,
    CONF_INCLUDE_TRADING_CARDS,
    CONF_FETCH_FLOATS,
    CONF_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_FETCH_CHUNK_SIZE,
    DEFAULT_STRICT_RATIO,
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_ITEMS,
    DEFAULT_HISTORY_DAYS,
    KNOWN_MARKETABLE_APPS,
    STORAGE_KEY,
    STORAGE_VERSION,
    WATCHLIST_FILE,
    TARGETS_FILE,
)
from .slugify import make_slug

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_INTERVAL = datetime.timedelta(days=7)
_SNAPSHOT_KEEP_DAYS = 10


def _parse_steam_ids(raw: str) -> list[tuple[str, str]]:
    accounts = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sid, name = part.split(":", 1)
            accounts.append((sid.strip(), name.strip()))
        else:
            accounts.append((part, f"account_{part[-8:]}"))
    return accounts


def _load_json_prices(config_dir: str, filename: str) -> dict[str, float]:
    """Load buy or reference prices from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}


def _load_json_list(config_dir: str, filename: str) -> list[dict]:
    """Load a JSON list from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return []


def _load_json_targets(config_dir: str, filename: str) -> dict[str, dict]:
    """Load price targets from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}


class CS2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches Steam inventories + prices for all detected games."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        cfg = {**entry.data, **entry.options}
        interval = cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(minutes=interval),
        )
        self._cfg = cfg
        self._store = storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._entity_pictures: dict[str, str] = {}
        self._current_prices: dict[str, float] = {}
        self._previous_prices: dict[str, float] = {}
        self._active_apps: list[tuple[int, int, str, str]] = []
        self._last_discovery: datetime.datetime | None = None
        self._price_snapshots: dict[str, dict[str, float]] = {}
        self._float_cache: dict[str, float] = {}
        self._price_timestamps: dict[str, float] = {}  # name → epoch of last fetch attempt
        self._last_cycle_stats: dict[str, Any] = {}
        self._alert_state: dict[str, str] = {}  # name → "high" | "low" | "none"
        self._inv_cooldown: dict[str, float] = {}  # steam_id → epoch when ban expires
        self._stop = threading.Event()
        self._import_running: bool = False
        self.config_entry = entry

    def stop(self) -> None:
        """Signal running executor thread to stop sleeping."""
        self._stop.set()

    @property
    def accounts(self) -> list[tuple[str, str]]:
        return _parse_steam_ids(self._cfg.get(CONF_STEAM_IDS, ""))

    @property
    def min_item_value(self) -> float:
        return float(self._cfg.get(CONF_MIN_ITEM_VALUE, DEFAULT_MIN_VALUE))

    @property
    def include_trading_cards(self) -> bool:
        return bool(self._cfg.get(CONF_INCLUDE_TRADING_CARDS, False))

    @property
    def fetch_floats(self) -> bool:
        return bool(self._cfg.get(CONF_FETCH_FLOATS, False))

    @property
    def history_days(self) -> int:
        return int(self._cfg.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))

    @property
    def last_cycle_stats(self) -> dict[str, Any]:
        return dict(self._last_cycle_stats)

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _async_load_store(self) -> None:
        data = await self._store.async_load() or {}
        self._entity_pictures = data.get("entity_pictures", {})
        self._current_prices = data.get("current_prices", {})
        self._previous_prices = data.get("previous_prices", {})
        raw_apps = data.get("active_apps", [])
        self._active_apps = [tuple(a) for a in raw_apps]
        last_disc = data.get("last_discovery")
        if last_disc:
            dt = datetime.datetime.fromisoformat(last_disc)
            self._last_discovery = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
        else:
            self._last_discovery = None
        self._price_snapshots = data.get("price_snapshots", {})
        self._float_cache = data.get("float_cache", {})
        self._alert_state = data.get("alert_state", {})
        self._price_timestamps = data.get("price_timestamps", {})
        self._inv_cooldown = {k: float(v) for k, v in data.get("inv_cooldown", {}).items()}

    async def _async_save_store(
        self,
        new_prices: dict[str, float],
        active_apps: list[tuple[int, int, str, str]],
        price_snapshots: dict[str, dict[str, float]],
        float_cache: dict[str, float],
    ) -> None:
        self._previous_prices = {**self._current_prices}
        # Merge new prices into current — never overwrite known prices with 0
        # (market 429 cycles return empty/zero prices; preserve last good values)
        merged = {**self._current_prices}
        merged.update({k: v for k, v in new_prices.items() if v > 0})
        self._current_prices = merged
        self._active_apps = active_apps
        self._last_discovery = dt_util.utcnow()
        self._price_snapshots = price_snapshots
        self._float_cache = float_cache
        # _price_timestamps and _entity_pictures are instance state — not passed as args
        try:
            await self._store.async_save(
                {
                    "entity_pictures": self._entity_pictures,
                    "current_prices": self._current_prices,
                    "previous_prices": self._previous_prices,
                    "active_apps": [list(a) for a in active_apps],
                    "last_discovery": self._last_discovery.isoformat(),
                    "price_snapshots": price_snapshots,
                    "float_cache": float_cache,
                    "alert_state": dict(self._alert_state),
                    "price_timestamps": self._price_timestamps,
                    "inv_cooldown": {k: v for k, v in self._inv_cooldown.items() if v > time.time()},
                }
            )
        except Exception as err:
            _LOGGER.error("Failed to persist coordinator state: %s", err)

    # ── Core update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        if self._stop.is_set():
            raise UpdateFailed("Coordinator has been stopped")
        await self._async_load_store()
        try:
            result, save_payload = await self.hass.async_add_executor_job(self._sync_cycle)
            if save_payload:
                await self._async_save_store(*save_payload)
            return result
        except steam_inventory.InventoryPrivateError as err:
            raise UpdateFailed(f"Steam inventory private: {err}") from err
        except steam_inventory.InventoryFetchError as err:
            # Transient IP ban — keep showing last known values instead of error state
            _LOGGER.warning("Inventory fetch failed: %s — reusing stale data", err)
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Steam inventory fetch error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Cycle failed: {err}") from err

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _needs_discovery(self) -> bool:
        if not self._active_apps:
            return True
        if self._last_discovery is None:
            return True
        return (dt_util.utcnow() - self._last_discovery) > _DISCOVERY_INTERVAL

    def _discover_active_apps(
        self, http: httpx.Client
    ) -> list[tuple[int, int, str, str]]:
        """Probe known app IDs to find which ones have non-empty inventories."""
        include_cards = self.include_trading_cards
        candidates = [
            app for app in KNOWN_MARKETABLE_APPS
            if app[0] != 753 or include_cards
        ]
        active: dict[str, tuple[int, int, str, str]] = {}

        for steam_id, _ in self.accounts:
            for appid, contextid, slug, game_name in candidates:
                if slug in active:
                    continue
                count = steam_inventory.check_inventory_count(
                    http, steam_id, appid, contextid, stop=self._stop
                )
                if count > 0:
                    active[slug] = (appid, contextid, slug, game_name)
                    _LOGGER.info(
                        "Discovery: found %s (appid=%d, %d items)",
                        game_name, appid, count,
                    )
                if self._stop.wait(0.5):
                    _LOGGER.info("Discovery interrupted by stop signal — preserving cached apps")
                    return list(self._active_apps) if self._active_apps else list(active.values())

        result = list(active.values())
        _LOGGER.info("Discovery complete: %d active games", len(result))
        return result

    # ── CSGOFloat ─────────────────────────────────────────────────────────────

    def _fetch_floats_for_game(
        self, http: httpx.Client, items: list[dict], appid: int
    ) -> dict[str, float]:
        """Fetch float values for CS2 skins (appid=730) with inspect links."""
        if appid != 730 or not self.fetch_floats:
            return {}
        from .api.csgofloat import fetch_floats
        floats = fetch_floats(http, items, cached=self._float_cache, stop=self._stop)
        # Update persistent cache (asset_id → float)
        for item in items:
            name = item.get("market_hash_name", "")
            asset_id = item.get("asset_id", name)
            if name in floats:
                self._float_cache[asset_id] = floats[name]
        return floats

    # ── Price thresholds ──────────────────────────────────────────────────────

    def _check_price_alerts(
        self,
        prices: dict[str, float],
        targets: dict[str, dict],
    ) -> None:
        """Fire HA events on price threshold state transitions (debounced)."""
        for name, thresholds in targets.items():
            price = prices.get(name)
            if price is None:
                continue
            high = thresholds.get("high")
            low = thresholds.get("low")

            new_state = "none"
            if high is not None and price >= high:
                new_state = "high"
            elif low is not None and price <= low:
                new_state = "low"

            prev_state = self._alert_state.get(name, "none")
            if new_state == prev_state:
                continue  # no transition — skip to avoid spam

            self._alert_state[name] = new_state
            if new_state == "none":
                continue  # price returned to normal range — no event fired

            threshold_value = high if new_state == "high" else low
            self.hass.loop.call_soon_threadsafe(
                self.hass.bus.async_fire,
                "steam_price_alert",
                {
                    "market_hash_name": name,
                    "current_price": price,
                    "threshold_type": new_state,
                    "threshold_value": threshold_value,
                },
            )
            _LOGGER.info(
                "Price alert %s: %s = %.2f (threshold %.2f)",
                new_state.upper(), name, price, threshold_value,
            )

    # ── Main cycle ────────────────────────────────────────────────────────────

    def _sync_cycle(self) -> tuple[dict[str, Any], tuple | None]:
        """Synchronous cycle — runs in executor thread. Returns (result, save_payload)."""
        cycle_start = time.monotonic()
        cfg = self._cfg
        cap = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        strict_ratio = float(cfg.get(CONF_STRICT_MISSING_RATIO, DEFAULT_STRICT_RATIO))
        min_val = self.min_item_value
        config_dir = self.hass.config.config_dir

        buy_prices = _load_json_prices(config_dir, "cs2_buy_prices.json")
        reference_prices = _load_json_prices(config_dir, "cs2_reference_prices.json")
        tracked_extras = set(buy_prices) | set(reference_prices)

        watchlist = _load_json_list(config_dir, WATCHLIST_FILE)
        price_targets = _load_json_targets(config_dir, TARGETS_FILE)

        # Compute 24h / 7d reference dates
        today = datetime.date.today().isoformat()
        date_24h = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        date_7d = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        prices_24h = self._price_snapshots.get(date_24h, {})
        prices_7d = self._price_snapshots.get(date_7d, {})

        with httpx.Client(http2=False) as http:
            # ── Discovery ─────────────────────────────────────────────────────
            active_apps = list(self._active_apps)
            if self._needs_discovery():
                active_apps = self._discover_active_apps(http)

            if not active_apps:
                return _empty_result(), None

            previous_prices = dict(self._previous_prices)
            limits = RateLimits.coordinator()
            per_game_data: dict[str, dict] = {}
            all_items_flat: list[dict] = []
            all_fresh_prices: dict[str, float] = {}
            total_stale = 0
            total_missing = 0

            for appid, contextid, slug, game_name in active_apps:
                if self._stop.is_set():
                    break

                # ── Fetch inventories for this game ────────────────────────────
                # Keep raw list (duplicates = owned copies) so compute_item_metrics
                # can count quantity correctly per unique market_hash_name.
                raw_items: list[dict] = []
                accounts_ok = 0
                for steam_id, account_name in self.accounts:
                    banned_until = self._inv_cooldown.get(steam_id, 0.0)
                    if time.time() < banned_until:
                        remaining = int(banned_until - time.time())
                        _LOGGER.info(
                            "Skipping %s inventory (%s) — IP ban cooldown %ds remaining",
                            account_name, game_name, remaining,
                        )
                        continue
                    try:
                        raw = steam_inventory.fetch_inventory(
                            http, steam_id, app_id=appid, context_id=contextid,
                            stop=self._stop,
                        )
                        accounts_ok += 1
                    except InventoryBannedError as err:
                        cooldown_until = time.time() + 3600
                        self._inv_cooldown[steam_id] = cooldown_until
                        _LOGGER.warning(
                            "Inventory 401 for %s (%s) — setting 1h cooldown until %s",
                            account_name, game_name,
                            time.strftime("%H:%M", time.localtime(cooldown_until)),
                        )
                        continue
                    except (steam_inventory.InventoryFetchError, steam_inventory.InventoryPrivateError) as err:
                        _LOGGER.warning("Inventory fetch failed for %s: %s — skipping", account_name, err)
                        continue
                    trackable = [
                        item for item in raw
                        if item.get("marketable")
                        or item["market_hash_name"] in tracked_extras
                    ]
                    for item in trackable:
                        raw_items.append(item)
                        pic = item.get("entity_picture")
                        if pic:
                            self._entity_pictures[item["market_hash_name"]] = pic

                if accounts_ok == 0 and self.accounts:
                    _LOGGER.warning("All %d accounts failed inventory for %s — skipping", len(self.accounts), game_name)
                    continue

                inventory = raw_items
                if cap > 0:
                    # Sort by best known price descending so cap keeps most-valuable items.
                    # Include _current_prices so items fetched in previous cycles are
                    # ordered correctly even when missing from buy/reference price files.
                    seen_order: dict[str, int] = {}
                    for item in inventory:
                        name = item["market_hash_name"]
                        if name not in seen_order:
                            seen_order[name] = max(
                                buy_prices.get(name, 0.0),
                                reference_prices.get(name, 0.0),
                                self._current_prices.get(name, 0.0),
                            )
                    inventory = sorted(
                        inventory,
                        key=lambda i: seen_order.get(i["market_hash_name"], 0.0),
                        reverse=True,
                    )
                # Unique names in order (preserve first-seen order for cap slice)
                unique_names = list(dict.fromkeys(i["market_hash_name"] for i in inventory))
                names_to_fetch = unique_names[:cap] if cap > 0 else unique_names
                names_set = set(names_to_fetch)

                # ── Fetch floats (CS2 only, opt-in) ───────────────────────────
                floats = self._fetch_floats_for_game(
                    http,
                    [i for i in inventory if i["market_hash_name"] in names_set],
                    appid,
                )

                # ── Rolling price fetch ────────────────────────────────────────
                # Pick the CHUNK_SIZE items with the oldest fetch timestamp so
                # every item gets refreshed roughly every (N/CHUNK_SIZE * interval).
                # Items never fetched have timestamp 0 and always go first.
                sorted_by_age = sorted(
                    names_to_fetch,
                    key=lambda n: self._price_timestamps.get(n, 0.0),
                )
                chunk = sorted_by_age[:DEFAULT_FETCH_CHUNK_SIZE]
                attempted: list[str] = []

                def _on_progress(
                    idx: int, total: int, name: str, price: float | None
                ) -> None:
                    attempted.append(name)

                fresh_chunk = steam_market.fetch_prices_parallel(
                    http, chunk,
                    on_progress=_on_progress,
                    limits=limits, stop=self._stop, app_id=appid,
                )

                now_ts = time.time()
                for name in attempted:
                    self._price_timestamps[name] = now_ts

                # Prices for metrics: accumulated current_prices + fresh overrides.
                # Items outside this cycle's chunk show their last stored price.
                prices: dict[str, float] = {
                    n: self._current_prices[n]
                    for n in names_to_fetch
                    if self._current_prices.get(n, 0.0) > 0
                }
                prices.update({k: v for k, v in fresh_chunk.items() if v > 0})

                stale_used = [n for n in names_to_fetch if n not in fresh_chunk]
                still_missing = [n for n in names_to_fetch if n not in prices]

                _LOGGER.debug(
                    "%s: rolling chunk %d/%d items — %d fresh, %d stale, %d missing",
                    game_name, len(chunk), len(names_to_fetch),
                    len(fresh_chunk), len(stale_used) - (len(chunk) - len(fresh_chunk)),
                    len(still_missing),
                )

                total_stale += len(stale_used)
                total_missing += len(still_missing)

                ratio = len(still_missing) / max(len(names_to_fetch), 1)
                if ratio > strict_ratio:
                    _LOGGER.warning(
                        "%s: %d/%d prices missing (%.0f%%>strict %.0f%%), using stale",
                        game_name, len(still_missing), len(names_to_fetch),
                        ratio * 100, strict_ratio * 100,
                    )

                fresh_prices = {k: v for k, v in prices.items() if k not in stale_used}
                all_fresh_prices.update(fresh_prices)

                # ── Compute per-game metrics ───────────────────────────────────
                items_data = compute_item_metrics(
                    inventory=[i for i in inventory if i["market_hash_name"] in names_set],
                    prices=prices,
                    floats=floats,
                    previous_prices=previous_prices,
                    buy_prices=buy_prices,
                    reference_prices=reference_prices,
                    prices_24h=prices_24h,
                    prices_7d=prices_7d,
                )

                for item in items_data:
                    if not item.get("entity_picture"):
                        item["entity_picture"] = self._entity_pictures.get(item["name"])
                    item["game_slug"] = slug
                    item["game_name"] = game_name

                if min_val > 0:
                    items_data = [
                        i for i in items_data
                        if (i.get("current_price") or 0) >= min_val
                        or (i.get("buy_price") or 0) >= min_val
                    ]

                if not items_data:
                    _LOGGER.info(
                        "Skipping %s — no items above min_value=%.2f EUR",
                        game_name, min_val,
                    )
                    continue

                prev_total = self._previous_total(previous_prices, items_data)
                game_metrics = compute_global_metrics(items_data, previous_total=prev_total)

                per_game_data[slug] = {
                    "appid": appid,
                    "name": game_name,
                    "slug": slug,
                    "items": items_data,
                    "metrics": game_metrics,
                }
                all_items_flat.extend(items_data)

            # If all games were skipped (IP ban), preserve last known coordinator state
            if not all_items_flat and active_apps and not self._stop.is_set():
                _LOGGER.warning("All %d active games skipped this cycle (IP ban?) — will use stale coordinator data", len(active_apps))
                raise steam_inventory.InventoryFetchError("All games skipped — using stale coordinator data")

            # ── Watchlist prices ───────────────────────────────────────────────
            watchlist_prices: dict[str, float] = {}
            watchlist_names = [
                w["market_hash_name"] for w in watchlist
                if "market_hash_name" in w
                and w["market_hash_name"] not in all_fresh_prices
            ]
            if watchlist_names and not self._stop.is_set():
                watchlist_prices = steam_market.fetch_prices_parallel(
                    http, watchlist_names,
                    limits=limits, stop=self._stop, app_id=730,
                )
                all_fresh_prices.update(watchlist_prices)

            # ── Price threshold alerts ─────────────────────────────────────────
            if price_targets:
                # Include stale prices so alerts fire even during Steam rate-limit periods
                combined_prices = {**self._current_prices, **all_fresh_prices}
                self._check_price_alerts(combined_prices, price_targets)
                # Purge _alert_state entries for removed targets to avoid unbounded growth
                self._alert_state = {k: v for k, v in self._alert_state.items() if k in price_targets}

            # ── Global metrics ─────────────────────────────────────────────────
            prev_global = self._previous_total(previous_prices, all_items_flat)
            global_metrics = compute_global_metrics(
                all_items_flat, previous_total=prev_global
            )

            # ── Price snapshot for today ───────────────────────────────────────
            snapshots = {**self._price_snapshots, today: dict(all_fresh_prices)}
            cutoff = (
                datetime.date.today() - datetime.timedelta(days=_SNAPSHOT_KEEP_DAYS)
            ).isoformat()
            snapshots = {k: v for k, v in snapshots.items() if k >= cutoff}

        # Snapshot float_cache AFTER game loop so newly fetched floats are persisted
        float_cache = dict(self._float_cache)
        # Purge entity_pictures for items no longer in any inventory
        if all_items_flat:
            active_names = {i["name"] for i in all_items_flat}
            self._entity_pictures = {k: v for k, v in self._entity_pictures.items() if k in active_names}
        cycle_duration = round(time.monotonic() - cycle_start, 1)

        now = time.time()
        banned_accounts = [sid for sid, until in self._inv_cooldown.items() if now < until]
        self._last_cycle_stats = {
            "total_value": global_metrics["total_value"],
            "items_count": global_metrics["items_count"],
            "active_games": len(per_game_data),
            "stale_count": total_stale,
            "missing_count": total_missing,
            "cycle_duration_s": cycle_duration,
            "last_update": dt_util.now().isoformat(),
            "banned_accounts": len(banned_accounts),
        }

        _LOGGER.info(
            "Steam cycle: total=%.2f EUR, games=%d, items=%d, stale=%d, missing=%d, %.1fs",
            global_metrics["total_value"],
            len(per_game_data),
            global_metrics["items_count"],
            total_stale,
            total_missing,
            cycle_duration,
        )

        # Only persist when we have actual fresh data — skip empty cycles (IP ban) to preserve price history
        save_payload = (
            (all_fresh_prices, active_apps, snapshots, float_cache)
            if all_fresh_prices or all_items_flat
            else None
        )

        watchlist_data = [
            {
                "market_hash_name": w["market_hash_name"],
                "appid": w.get("appid", 730),
                # owned watched items have price in all_fresh_prices, not watchlist_prices
                "current_price": all_fresh_prices.get(w["market_hash_name"]) or watchlist_prices.get(w["market_hash_name"]),
                "target_price": w.get("target_price"),
                "note": w.get("note", ""),
                "slug": make_slug(w["market_hash_name"]),
            }
            for w in watchlist
            if "market_hash_name" in w
        ]
        return {
            "global": global_metrics,
            "per_game": per_game_data,
            "items": all_items_flat,
            "items_by_slug": {f"{i['game_slug']}__{i['slug']}": i for i in all_items_flat},
            "per_account": {},
            "active_apps": active_apps,
            "stale_count": total_stale,
            "missing_count": total_missing,
            "watchlist": watchlist_data,
            "watchlist_by_slug": {w["slug"]: w for w in watchlist_data},
        }, save_payload

    @staticmethod
    def _previous_total(previous_prices: dict, items_data: list[dict]) -> float | None:
        if not previous_prices:
            return None
        present = {i["name"] for i in items_data}
        qty_map = {i["name"]: i.get("quantity", 1) for i in items_data}
        total = sum(
            price * qty_map[name]
            for name, price in previous_prices.items()
            if name in present
        )
        return round(total, 2) if total else None


def _empty_result() -> dict[str, Any]:
    return {
        "global": compute_global_metrics([], previous_total=None),
        "per_game": {},
        "items": [],
        "items_by_slug": {},
        "per_account": {},
        "active_apps": [],
        "stale_count": 0,
        "missing_count": 0,
        "watchlist": [],
        "watchlist_by_slug": {},
    }
