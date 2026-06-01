"""HA storage persistence for the CS2 coordinator.

Separates all load/save logic from the coordinator so that the coordinator
holds pure business logic and this module owns the serialization contract.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

_LOGGER = logging.getLogger(__name__)

# When loading stored cooldowns, cap any entry that exceeds this offset from now.
# Prevents accounts from being permanently stuck if the system clock jumps forward
# (e.g., after a large NTP correction or VM snapshot restore).
_MAX_COOLDOWN_OFFSET = 7200  # 2 hours


def result_to_json(result: dict) -> str | None:
    """Serialize coordinator result payload for cold-restart persistence.

    Omits the derived lookup dicts (items_by_slug, watchlist_by_slug) — they
    are rebuilt by result_from_json so we don't store redundant data.
    """
    if not result:
        return None
    try:
        r = {
            "global": result.get("global", {}),
            "per_game": result.get("per_game", {}),
            "items": result.get("items", []),
            "active_apps": [list(a) for a in result.get("active_apps", [])],
            "stale_count": result.get("stale_count", 0),
            "missing_count": result.get("missing_count", 0),
            "watchlist": result.get("watchlist", []),
        }
        return json.dumps(r, ensure_ascii=False, default=str)
    except Exception:
        return None


def result_from_json(raw: str | None) -> dict | None:
    """Deserialize coordinator result payload from store."""
    if not raw:
        return None
    try:
        r = json.loads(raw)
        r["active_apps"] = [tuple(a) for a in r.get("active_apps", [])]
        items = r.get("items", [])
        r["items_by_slug"] = {
            f"{i.get('game_slug')}__{i.get('slug')}": i for i in items
        }
        watchlist = r.get("watchlist", [])
        r["watchlist_by_slug"] = {w["slug"]: w for w in watchlist if w.get("slug")}
        r["per_account"] = {}
        return r
    except Exception:
        return None


class CS2Store:
    """Wraps HA storage with coordinator-specific serialization and merge logic."""

    def __init__(self, hass: HomeAssistant, key: str, version: int) -> None:
        self._store = storage.Store(hass, version, key)

    async def async_load(
        self, current_inv_cooldown: dict[str, float]
    ) -> dict[str, Any]:
        """Load coordinator state from HA storage.

        Returns a flat dict of state values. The coordinator applies them to
        its own attributes.

        ``current_inv_cooldown`` contains in-memory bans from this session;
        these win on merge so a ban set this session is never rolled back by a
        stale store value.
        """
        data = await self._store.async_load() or {}
        now = time.time()

        # Load stored cooldowns, capping each to prevent stuck bans from clock jumps
        stored_cd: dict[str, float] = {}
        for k, v in data.get("inv_cooldown", {}).items():
            ts = float(v)
            stored_cd[k] = min(ts, now + _MAX_COOLDOWN_OFFSET)

        # In-memory active bans (set this session) take precedence
        active_mem = {k: v for k, v in current_inv_cooldown.items() if v > now}
        merged_cd = {k: v for k, v in {**stored_cd, **active_mem}.items() if v > now}

        raw_apps = data.get("active_apps", [])
        last_disc_raw = data.get("last_discovery")
        if last_disc_raw:
            dt = datetime.datetime.fromisoformat(last_disc_raw)
            last_discovery: datetime.datetime | None = (
                dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
            )
        else:
            last_discovery = None

        return {
            "entity_pictures": data.get("entity_pictures", {}),
            "current_prices": data.get("current_prices", {}),
            "previous_prices": data.get("previous_prices", {}),
            "active_apps": [tuple(a) for a in raw_apps],
            "last_discovery": last_discovery,
            "price_snapshots": data.get("price_snapshots", {}),
            "float_cache": data.get("float_cache", {}),
            "alert_state": data.get("alert_state", {}),
            "price_timestamps": data.get("price_timestamps", {}),
            "inv_cooldown": merged_cd,
            "stale_data": result_from_json(data.get("last_coordinator_result")),
        }

    async def async_save(
        self,
        *,
        entity_pictures: dict[str, str],
        current_prices: dict[str, float],
        previous_prices: dict[str, float],
        active_apps: list[tuple],
        last_discovery: datetime.datetime | None,
        price_snapshots: dict[str, dict[str, float]],
        float_cache: dict[str, float],
        alert_state: dict[str, str],
        price_timestamps: dict[str, float],
        inv_cooldown: dict[str, float],
        stale_data: dict | None,
    ) -> None:
        """Persist full coordinator state to HA storage."""
        now = time.time()
        try:
            await self._store.async_save(
                {
                    "entity_pictures": entity_pictures,
                    "current_prices": current_prices,
                    "previous_prices": previous_prices,
                    "active_apps": [list(a) for a in active_apps],
                    "last_discovery": last_discovery.isoformat() if last_discovery else None,
                    "price_snapshots": price_snapshots,
                    "float_cache": float_cache,
                    "alert_state": dict(alert_state),
                    "price_timestamps": price_timestamps,
                    "inv_cooldown": {k: v for k, v in inv_cooldown.items() if v > now},
                    "last_coordinator_result": result_to_json(stale_data),
                }
            )
        except Exception as err:
            _LOGGER.error("Failed to persist coordinator state: %s", err)

    async def async_save_cooldown(
        self,
        inv_cooldown: dict[str, float],
        stale_data: dict | None,
    ) -> None:
        """Fast-path: persist only cooldown (and opportunistically stale_data)."""
        now = time.time()
        try:
            data = await self._store.async_load() or {}
            data["inv_cooldown"] = {k: v for k, v in inv_cooldown.items() if v > now}
            # Only write stale_data if not already present (avoid overwriting a good snapshot)
            if stale_data and not data.get("last_coordinator_result"):
                data["last_coordinator_result"] = result_to_json(stale_data)
            await self._store.async_save(data)
        except Exception as err:
            _LOGGER.warning("Failed to persist cooldown state: %s", err)
