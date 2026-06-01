"""Item-name → entity-slug conversion.

Must match the HACS coordinator implementation **bit-for-bit** so that all
existing `sensor.cs2_item_*` entity_ids referenced by the 363 KB custom
dashboard (`dashboards/dashboard_cs2.yaml`) keep working after the cutover.

Original PowerShell rule (preserved through HACS):
    ($name -replace '[^a-zA-Z0-9]','_' -replace '_+','_').ToLower().Trim('_')
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_MULTI_UNDER = re.compile(r"_+")


def make_slug(name: str) -> str:
    """Convert a Steam market_hash_name to the HA entity slug used as suffix."""
    slug = _NON_ALNUM.sub("_", name)
    slug = _MULTI_UNDER.sub("_", slug)
    return slug.lower().strip("_")


