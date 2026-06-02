"""Regression tests for cs2.run_import guard cleanup."""
import asyncio
import builtins
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.cs2 import _run_import


def test_run_import_resets_running_flag_when_importer_import_fails(monkeypatch):
    """A failure before async_run_import must not leave _import_running stuck."""
    coordinator = SimpleNamespace(
        data={"items": [{"name": "AK-47 | Cartel", "current_price": 10.0}]},
        _import_running=True,
        _import_progress={},
    )
    hass = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args)),
    )

    real_import = builtins.__import__

    def fail_importer(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "importer" and level == 1:
            raise ImportError("simulated importer import failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_importer)

    asyncio.run(
        _run_import(
            hass,
            coordinator,
            cookie="steamLoginSecure=fake",
            start_date="2013-01-01",
        )
    )

    assert coordinator._import_running is False
    assert coordinator._import_progress["running"] is False
    assert "simulated importer import failure" in coordinator._import_progress["error"]
