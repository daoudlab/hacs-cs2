"""DataUpdateCoordinator for CS2/Steam Inventory."""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import steam_inventory, steam_market
from .api.steam_inventory import InventoryBannedError, InventoryRateLimitedError
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
from .price_tracker import RollingPriceFetcher
from .slugify import make_slug
from .state_store import CS2Store
from .utils import (
    load_json_list,
    load_json_prices,
    load_json_targets,
    parse_steam_ids,
)

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_INTERVAL = datetime.timedelta(days=7)
_SNAPSHOT_KEEP_DAYS = 10

# Market 429 backoff schedule (minutes): 5 → 15 → 30 → 60 (capped)
_MARKET_RL_BACKOFF_MINUTES = [5, 15, 30, 60]


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
        self._cs2_store = CS2Store(hass, STORAGE_KEY, STORAGE_VERSION)
        self._price_tracker = RollingPriceFetcher()

        self._entity_pictures: dict[str, str] = {}
        self._current_prices: dict[str, float] = {}
        self._previous_prices: dict[str, float] = {}
        self._active_apps: list[tuple[int, int, str, str]] = []
        self._last_discovery: datetime.datetime | None = None
        self._price_snapshots: dict[str, dict[str, float]] = {}
        self._float_cache: dict[str, float] = {}
        self._last_cycle_stats: dict[str, Any] = {}
        self._alert_state: dict[str, str] = {}
        self._inv_cooldown: dict[str, float] = {}
        self._market_rl_until: float = 0.0
        self._market_rl_consecutive: int = 0
        self._stale_data: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._import_running: bool = False
        self._import_progress: dict[str, Any] = {}
        self.config_entry = entry

    def stop(self) -> None:
        """Signal running executor thread to stop sleeping."""
        self._stop.set()

    @property
    def _device_unique_id(self) -> str:
        """Stable device identifier derived from configured Steam IDs.

        Using a hash of steam_ids instead of entry_id means the same Steam
        account always maps to the same HA device, even after a reinstall.
        """
        import hashlib
        raw = self._cfg.get(CONF_STEAM_IDS, "").strip()
        return hashlib.md5(raw.encode()).hexdigest()[:16] if raw else self.config_entry.entry_id

    @property
    def accounts(self) -> list[tuple[str, str]]:
        return parse_steam_ids(self._cfg.get(CONF_STEAM_IDS, ""))

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
        loaded = await self._cs2_store.async_load(self._inv_cooldown)
        self._entity_pictures = loaded["entity_pictures"]
        self._current_prices = loaded["current_prices"]
        self._previous_prices = loaded["previous_prices"]
        self._active_apps = loaded["active_apps"]
        self._last_discovery = loaded["last_discovery"]
        self._price_snapshots = loaded["price_snapshots"]
        self._float_cache = loaded["float_cache"]
        self._alert_state = loaded["alert_state"]
        self._price_tracker.load_timestamps(loaded["price_timestamps"])
        self._inv_cooldown = loaded["inv_cooldown"]
        self._market_rl_until = loaded["market_rl_until"]
        self._market_rl_consecutive = loaded["market_rl_consecutive"]
        if self._stale_data is None:
            self._stale_data = loaded["stale_data"]

    async def _async_save_store(
        self,
        new_prices: dict[str, float],
        active_apps: list[tuple[int, int, str, str]],
        price_snapshots: dict[str, dict[str, float]],
        float_cache: dict[str, float],
    ) -> None:
        self._previous_prices = dict(self._current_prices)
        # Merge new prices; never overwrite known values with 0 (empty 429 cycles)
        merged = {**self._current_prices}
        merged.update({k: v for k, v in new_prices.items() if v > 0})
        self._current_prices = merged
        self._active_apps = active_apps
        self._last_discovery = dt_util.utcnow()
        self._price_snapshots = price_snapshots
        self._float_cache = float_cache

        await self._cs2_store.async_save(
            entity_pictures=self._entity_pictures,
            current_prices=self._current_prices,
            previous_prices=self._previous_prices,
            active_apps=active_apps,
            last_discovery=self._last_discovery,
            price_snapshots=price_snapshots,
            float_cache=float_cache,
            alert_state=self._alert_state,
            price_timestamps=self._price_tracker.timestamps,
            inv_cooldown=self._inv_cooldown,
            stale_data=self._stale_data,
            market_rl_until=self._market_rl_until,
            market_rl_consecutive=self._market_rl_consecutive,
        )

    async def _async_save_cooldown_now(self) -> None:
        """Persist cooldowns and opportunistically stale_data (fast-path)."""
        await self._cs2_store.async_save_cooldown(
            self._inv_cooldown,
            self._stale_data,
            market_rl_until=self._market_rl_until,
            market_rl_consecutive=self._market_rl_consecutive,
        )

    # ── Core update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        if self._stop.is_set():
            raise UpdateFailed("Coordinator has been stopped")
        await self._async_load_store()
        try:
            result, save_payload = await self.hass.async_add_executor_job(self._sync_cycle)
            if result is None:
                # All games in cooldown — persist immediately, fall back to stale
                await self._async_save_cooldown_now()
                fallback = self.data or self._stale_data
                if fallback is not None:
                    _LOGGER.info("All games in IP ban cooldown — serving stale data")
                    return fallback
                _LOGGER.warning("All games in cooldown and no stale data — returning empty result")
                minimal = _empty_result()
                minimal["active_apps"] = list(self._active_apps)
                return minimal
            if save_payload:
                self._stale_data = result
                await self._async_save_store(*save_payload)
            else:
                self._stale_data = result
            return result
        except steam_inventory.InventoryPrivateError as err:
            raise UpdateFailed(f"Steam inventory private: {err}") from err
        except steam_inventory.InventoryFetchError as err:
            _LOGGER.warning("Inventory fetch failed: %s — reusing stale data", err)
            fallback = self.data or self._stale_data
            if fallback is not None:
                return fallback
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
                if count == -1:
                    # 429 during discovery — abort immediately to avoid ban escalation
                    _LOGGER.warning(
                        "Discovery rate-limited (429) — aborting, preserving %d cached apps",
                        len(self._active_apps),
                    )
                    return list(self._active_apps) if self._active_apps else list(active.values())
                if count > 0:
                    active[slug] = (appid, contextid, slug, game_name)
                    _LOGGER.info(
                        "Discovery: found %s (appid=%d, %d items)",
                        game_name, appid, count,
                    )
                if self._stop.wait(3.0):
                    _LOGGER.info("Discovery interrupted — preserving cached apps")
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
                continue  # price returned to normal — no event fired

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

        buy_prices = load_json_prices(config_dir, "cs2_buy_prices.json")
        reference_prices = load_json_prices(config_dir, "cs2_reference_prices.json")
        tracked_extras = set(buy_prices) | set(reference_prices)

        watchlist = load_json_list(config_dir, WATCHLIST_FILE)
        price_targets = load_json_targets(config_dir, TARGETS_FILE)

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
            cycle_asset_ids: set[str] = set()  # track for float_cache pruning

            # Market rate-limit state for this cycle
            market_rl_active = time.time() < self._market_rl_until
            market_rl_initially_active = market_rl_active
            cycle_had_market_429 = False

            for appid, contextid, slug, game_name in active_apps:
                if self._stop.is_set():
                    break

                # ── Fetch inventories ──────────────────────────────────────────
                raw_items: list[dict] = []
                accounts_ok = 0
                accounts_in_cooldown = 0
                for steam_id, account_name in self.accounts:
                    banned_until = self._inv_cooldown.get(steam_id, 0.0)
                    if time.time() < banned_until:
                        accounts_in_cooldown += 1
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
                    except InventoryBannedError:
                        cooldown_until = time.time() + 3600
                        self._inv_cooldown[steam_id] = cooldown_until
                        _LOGGER.warning(
                            "Inventory 401 for %s (%s) — 1h cooldown until %s",
                            account_name, game_name,
                            time.strftime("%H:%M", time.localtime(cooldown_until)),
                        )
                        continue
                    except InventoryRateLimitedError:
                        cooldown_until = time.time() + 900  # 15 min (shorter than 401 ban)
                        self._inv_cooldown[steam_id] = cooldown_until
                        _LOGGER.warning(
                            "Inventory 429 for %s (%s) — 15 min cooldown until %s",
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
                        cycle_asset_ids.add(item.get("asset_id", ""))
                        pic = item.get("entity_picture")
                        if pic:
                            self._entity_pictures[item["market_hash_name"]] = pic

                if accounts_ok == 0 and self.accounts:
                    if accounts_in_cooldown == len(self.accounts):
                        _LOGGER.info(
                            "All %d accounts in cooldown for %s — skipping",
                            len(self.accounts), game_name,
                        )
                    else:
                        _LOGGER.warning(
                            "All %d accounts failed inventory for %s — skipping",
                            len(self.accounts), game_name,
                        )
                    continue

                inventory = raw_items
                if cap > 0:
                    # Sort by best known price desc so cap keeps the most valuable items
                    seen_order: dict[str, float] = {}
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

                unique_names = list(dict.fromkeys(i["market_hash_name"] for i in inventory))
                names_to_fetch = unique_names[:cap] if cap > 0 else unique_names
                names_set = set(names_to_fetch)

                # ── Fetch floats (CS2 only, opt-in) ───────────────────────────
                floats = self._fetch_floats_for_game(
                    http,
                    [i for i in inventory if i["market_hash_name"] in names_set],
                    appid,
                )

                # ── Rolling price fetch (via RollingPriceFetcher) ──────────────
                if not market_rl_active:
                    fresh_chunk, circuit_broken = self._price_tracker.fetch(
                        http, names_to_fetch, limits, self._stop, appid
                    )
                    if circuit_broken:
                        cycle_had_market_429 = True
                        market_rl_active = True  # skip remaining games this cycle
                        idx = min(self._market_rl_consecutive, len(_MARKET_RL_BACKOFF_MINUTES) - 1)
                        cooldown_min = _MARKET_RL_BACKOFF_MINUTES[idx]
                        self._market_rl_until = time.time() + cooldown_min * 60
                        self._market_rl_consecutive += 1
                        _LOGGER.warning(
                            "Market 429 (consecutive hit #%d) — pausing market fetches for %d min (until ~%s)",
                            self._market_rl_consecutive,
                            cooldown_min,
                            time.strftime("%H:%M", time.localtime(self._market_rl_until)),
                        )
                else:
                    fresh_chunk = {}
                    remaining_s = int(self._market_rl_until - time.time())
                    _LOGGER.info(
                        "Market rate-limited (%ds remaining) — skipping price fetch for %s",
                        remaining_s, game_name,
                    )

                # Build prices dict: current_prices as baseline, fresh override
                prices: dict[str, float] = {
                    n: self._current_prices[n]
                    for n in names_to_fetch
                    if self._current_prices.get(n, 0.0) > 0
                }
                prices.update({k: v for k, v in fresh_chunk.items() if v > 0})

                # Stale = requested but not fresh-fetched this cycle
                # Missing = not in prices at all (no stored or fresh value)
                stale_names = [n for n in names_to_fetch if n not in fresh_chunk]
                missing_names = [n for n in names_to_fetch if n not in prices]

                _LOGGER.debug(
                    "%s: chunk %d/%d — %d fresh, %d stale, %d missing",
                    game_name,
                    min(len(names_to_fetch), self._price_tracker._chunk_size),
                    len(names_to_fetch),
                    len(fresh_chunk),
                    len(stale_names) - len(missing_names),
                    len(missing_names),
                )

                total_stale += len(stale_names)
                total_missing += len(missing_names)

                ratio = len(missing_names) / max(len(names_to_fetch), 1)
                if ratio > strict_ratio:
                    _LOGGER.warning(
                        "%s: %d/%d prices missing (%.0f%% > strict %.0f%%), using stale",
                        game_name, len(missing_names), len(names_to_fetch),
                        ratio * 100, strict_ratio * 100,
                    )

                # Only fresh prices contribute to today's snapshot and watchlist
                all_fresh_prices.update(fresh_chunk)

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
                    item["appid"] = appid

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

            # If all games were skipped (IP ban cooldown), signal caller to use stale
            if not all_items_flat and active_apps and not self._stop.is_set():
                _LOGGER.warning(
                    "All %d active games skipped this cycle (IP ban) — returning None for stale fallback",
                    len(active_apps),
                )
                return None, None

            # ── Prune stale entries ────────────────────────────────────────────
            active_names = {i["name"] for i in all_items_flat}
            watchlist_names_set = {
                w["market_hash_name"] for w in watchlist if "market_hash_name" in w
            }
            # price_timestamps: keep only active inventory + watchlist names
            self._price_tracker.prune(active_names | watchlist_names_set)
            # current_prices: prune items removed from inventory and watchlist
            keep_names = active_names | watchlist_names_set | set(buy_prices) | set(reference_prices)
            self._current_prices = {k: v for k, v in self._current_prices.items() if k in keep_names}
            # float_cache: keep only asset_ids seen in this cycle
            if cycle_asset_ids:
                self._float_cache = {
                    k: v for k, v in self._float_cache.items() if k in cycle_asset_ids
                }
            # entity_pictures: keep only active inventory names
            if active_names:
                self._entity_pictures = {
                    k: v for k, v in self._entity_pictures.items() if k in active_names
                }

            # ── Watchlist prices ───────────────────────────────────────────────
            watchlist_prices: dict[str, float] = {}
            watchlist_fetch_names = [
                w["market_hash_name"] for w in watchlist
                if "market_hash_name" in w
                and w["market_hash_name"] not in all_fresh_prices
            ]
            if watchlist_fetch_names and not self._stop.is_set() and not market_rl_active:
                wl_prices, wl_circuit_broken = steam_market.fetch_prices_parallel(
                    http, watchlist_fetch_names,
                    limits=limits, stop=self._stop, app_id=730,
                )
                if wl_circuit_broken and not cycle_had_market_429:
                    cycle_had_market_429 = True
                    idx = min(self._market_rl_consecutive, len(_MARKET_RL_BACKOFF_MINUTES) - 1)
                    cooldown_min = _MARKET_RL_BACKOFF_MINUTES[idx]
                    self._market_rl_until = time.time() + cooldown_min * 60
                    self._market_rl_consecutive += 1
                    _LOGGER.warning(
                        "Market 429 on watchlist (consecutive hit #%d) — pausing for %d min",
                        self._market_rl_consecutive, cooldown_min,
                    )
                watchlist_prices = wl_prices
                all_fresh_prices.update(watchlist_prices)

            # Reset market RL consecutive counter after a fully clean cycle
            if not market_rl_initially_active and not cycle_had_market_429:
                if self._market_rl_consecutive > 0:
                    _LOGGER.info("Market rate-limit lifted — resetting backoff counter")
                self._market_rl_consecutive = 0
                self._market_rl_until = 0.0

            # ── Price threshold alerts ─────────────────────────────────────────
            if price_targets:
                combined_prices = {**self._current_prices, **all_fresh_prices}
                self._check_price_alerts(combined_prices, price_targets)
                self._alert_state = {
                    k: v for k, v in self._alert_state.items() if k in price_targets
                }

            # ── Global metrics ─────────────────────────────────────────────────
            prev_global = self._previous_total(previous_prices, all_items_flat)
            global_metrics = compute_global_metrics(all_items_flat, previous_total=prev_global)

            # ── Price snapshot for today ───────────────────────────────────────
            snapshots = {**self._price_snapshots, today: dict(all_fresh_prices)}
            cutoff = (
                datetime.date.today() - datetime.timedelta(days=_SNAPSHOT_KEEP_DAYS)
            ).isoformat()
            snapshots = {k: v for k, v in snapshots.items() if k >= cutoff}

        float_cache = dict(self._float_cache)
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
            "market_rl_until": self._market_rl_until,
            "market_rl_consecutive": self._market_rl_consecutive,
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

        save_payload = (
            (all_fresh_prices, active_apps, snapshots, float_cache)
            if all_fresh_prices or all_items_flat
            else None
        )

        watchlist_data = [
            {
                "market_hash_name": w["market_hash_name"],
                "appid": w.get("appid", 730),
                "current_price": (
                    all_fresh_prices.get(w["market_hash_name"])
                    or watchlist_prices.get(w["market_hash_name"])
                ),
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
