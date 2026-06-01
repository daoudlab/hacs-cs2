"""Test configuration — mock HA and third-party deps for unit tests.

The pure-function tests (compute, slugify, steam_market, utils) don't need
a real HA runtime.  We mock every HA module before any test module loads so
that the package __init__.py doesn't fail on missing deps.
"""
import sys
from unittest.mock import MagicMock

_HA_MODULES = [
    "voluptuous",
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.entity_registry",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.recorder",
    "homeassistant.components.recorder.models",
    "homeassistant.components.recorder.statistics",
    "homeassistant.util",
    "homeassistant.util.dt",
]

for _mod in _HA_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Ensure voluptuous.Invalid is importable as an exception
import voluptuous as _vol  # noqa: E402 (now mocked)
if not isinstance(_vol.Invalid, type):
    _vol.Invalid = type("Invalid", (Exception,), {})
