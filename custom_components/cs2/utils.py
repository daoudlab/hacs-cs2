"""Shared utility helpers for the CS2/Steam Inventory integration."""
from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def parse_steam_ids(raw: str) -> list[tuple[str, str]]:
    """Parse 'steamid:name,steamid:name' or plain comma-separated Steam IDs."""
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


def load_json_prices(config_dir: str, filename: str) -> dict[str, float]:
    """Load buy or reference prices from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            _LOGGER.warning("Expected a JSON object in %s, got %s — ignoring", path, type(data).__name__)
            return {}
        return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}


def load_json_list(config_dir: str, filename: str) -> list[dict]:
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


def load_json_targets(config_dir: str, filename: str) -> dict[str, dict]:
    """Load price targets from {config_dir}/{filename} if present."""
    path = Path(config_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as err:
        _LOGGER.warning("Failed to load %s: %s", path, err)
        return {}
