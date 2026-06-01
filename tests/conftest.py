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
    "homeassistant.exceptions",
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

# Ensure homeassistant.exceptions.HomeAssistantError is a real exception class
import homeassistant.exceptions as _ha_exc  # noqa: E402
if not isinstance(_ha_exc.HomeAssistantError, type):
    _ha_exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

# Replace DataUpdateCoordinator with a real Python class so CS2Coordinator
# inherits from a proper type rather than a MagicMock instance.
# Without this, CS2Coordinator's staticmethods and regular methods become
# inaccessible because MagicMock.__mro_entries__ causes CS2Coordinator to
# inherit from MagicMock (the class), which intercepts all attribute lookups.
#
# IMPORTANT: do NOT use `import homeassistant.helpers.update_coordinator as _upd_coord`
# here. The `import` statement triggers Python's package hierarchy traversal,
# which calls setattr on the parent MagicMock (homeassistant.helpers), binding a
# *fresh* child MagicMock as .update_coordinator. A later import of coordinator.py
# then picks up that fresh child mock instead of the sys.modules entry we patched,
# causing CS2Coordinator to inherit from MagicMock. Access sys.modules directly.
_upd_coord = sys.modules["homeassistant.helpers.update_coordinator"]
# Also bind the child on the parent mock to prevent fresh child creation
sys.modules["homeassistant.helpers"].update_coordinator = _upd_coord


class _FakeDataUpdateCoordinator:
    """Minimal stub — just enough for CS2Coordinator to inherit cleanly."""
    def __init__(self, hass=None, logger=None, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True

    def async_add_listener(self, update_callback, context=None):
        return lambda: None

    def __class_getitem__(cls, item):
        return cls


_upd_coord.DataUpdateCoordinator = _FakeDataUpdateCoordinator
_upd_coord.UpdateFailed = type("UpdateFailed", (Exception,), {})
